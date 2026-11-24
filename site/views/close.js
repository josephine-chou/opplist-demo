// Won or Lost: a colleague says how a case ended and adds the mail that says so, pasted here (screenshots, the
// mail's text) or forwarded to the administrator. It does not close the case: it is an action of type close that
// OppList takes in as a suggestion, and the case changes when the administrator accepts it. Until then the case
// reads as waiting. The pasted mail goes into evidence/{the action's id}, which only the administrator can
// read. Screenshots are made smaller here, as JPEG, so the whole record stays under the store's limit.
import { S, $, modal } from "../core.js";
import { esc, todayIso, validIso, plural } from "../lib.js";
import { act, getEvidence } from "../data.js";
import { failHtml } from "../issues.js";

export const MAX_IMAGES = 3;
export const MAX_TEXT = 5000;
const MAX_TOTAL = 900_000;           // characters of the pasted record together (a store record holds about a million)
const STEPS = [[1600, 0.82], [1400, 0.76], [1200, 0.7], [1000, 0.65], [800, 0.6], [640, 0.55]];

export const closeLabel = pc => `${pc.result}? waiting for Iris`;
// A Lost because nobody answered needs no mail. The form hides the mail part and sends the
// reason itself as the pasted text, so OppList takes it in (and leaves the Timeline note empty).
export const NO_MAIL = /^No response\b/i;
export const reasons = () => S.meta?.lostReasons || null;

// the form, drawn into the case page's panel; done(msg) is called after it is sent
export function closeForm(box, opp, c, { onCancel, onDone, rateDefault = "" }) {
  const today = todayIso(), lost = reasons() || [];
  const quoted = new Map();
  for (const r of c?.rr || []) if (r.vendor && !r.pending) quoted.set(r.vendor, quoted.get(r.vendor) || !!r.quoted);
  const vendors = [...quoted].sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]));
  let images = [];
  box.innerHTML = `<form class="sub-panel close-form" id="f-p" novalidate><h3>Won or Lost</h3>
      <fieldset class="field"><legend>How did it end?</legend>
        <span class="pills"><label class="pill-radio"><input type="radio" name="result" value="Won"> Won</label><label class="pill-radio"><input type="radio" name="result" value="Lost"> Lost</label></span></fieldset>
      <label class="field">Date<input type="date" name="date" value="${today}" max="${today}" required></label>
      <label class="field" data-show="Lost" hidden>Reason<select name="reason"><option value="">Pick the reason</option>${lost.map(x => `<option>${esc(x)}</option>`).join("")}</select></label>
      <label class="field" data-show="Won" hidden>Whose rate was used<select name="rateFrom"><option value="">Not sure</option><option value="own"${rateDefault === "own" ? " selected" : ""}>Our own rate</option>
        ${vendors.map(([k, q]) => `<option value="${esc(k)}"${k === rateDefault ? " selected" : ""}>${esc(k)}${q ? " (quoted)" : ""}</option>`).join("")}</select></label>
      <fieldset class="field ev-field"><legend>The mail that says so</legend>
        <label class="radio"><input type="radio" name="ev" value="pasted" checked> Paste it here</label>
        <label class="radio"><input type="radio" name="ev" value="forwarded"> I forwarded it to Iris</label></fieldset>
      <div class="ev-pasted">
        <div class="paste-zone" id="cl-zone" tabindex="0" role="button" aria-label="Paste a screenshot of the mail here">
          <b>Click here, then press Ctrl+V</b> to paste a screenshot of the mail (Windows+Shift+S takes one). Up to ${MAX_IMAGES}.
          <span class="muted"> Or <button type="button" class="linkbtn" id="cl-pick">choose a file</button>.</span>
          <input type="file" id="cl-file" accept="image/*" multiple hidden></div>
        <div class="thumbs" id="cl-thumbs"></div>
        <label class="field">Or paste the mail's text<textarea name="text" rows="4" maxlength="${MAX_TEXT}" placeholder="From, date and the lines that say Won or Lost"></textarea></label>
        <p class="muted small m0" id="cl-size"></p>
      </div>
      <p class="note-box small ev-fwd" hidden>Forward the mail to Iris with <b>[${esc(opp)}]</b> in the subject, so it is matched to this case.</p>
      <p class="muted small m0 ev-none" hidden>No mail needed when nobody answered.</p>
      <div class="err" id="p-err"></div>
      <div class="row-end"><button type="button" class="btn" data-cancel>Cancel</button><button type="submit" class="btn primary">Submit</button></div></form>`;
  const f = $("#f-p"), err = $("#p-err");
  const used = () => images.reduce((n, x) => n + x.length, 0) + f.text.value.length;
  function paint() {
    $("#cl-thumbs").innerHTML = images.map((src, i) => `<figure><img src="${src}" alt="Screenshot ${i + 1}"><button type="button" class="xbtn small" data-rm="${i}" aria-label="Remove screenshot ${i + 1}">&times;</button></figure>`).join("");
    const kb = Math.round(used() / 1000);
    $("#cl-size").textContent = images.length || f.text.value ? `${plural(images.length, "screenshot")}${f.text.value ? " and the text" : ""} · ${kb} KB of ${MAX_TOTAL / 1000}` : "";
    $("#cl-zone").classList.toggle("full", images.length >= MAX_IMAGES);
  }
  function show() {
    const result = f.result.value, ev = f.ev.value;
    box.querySelectorAll("[data-show]").forEach(x => { x.hidden = x.dataset.show !== result; });
    const none = result === "Lost" && NO_MAIL.test(f.reason.value);
    box.querySelector(".ev-field").hidden = none;
    box.querySelector(".ev-none").hidden = !none;
    box.querySelector(".ev-pasted").hidden = none || ev !== "pasted";
    box.querySelector(".ev-fwd").hidden = none || ev !== "forwarded";
  }
  async function add(files) {
    err.textContent = "";
    for (const file of files) {
      if (!file || !/^image\//.test(file.type)) continue;
      if (images.length >= MAX_IMAGES) { err.textContent = `Up to ${MAX_IMAGES} screenshots.`; break; }
      try {
        const url = await shrink(file, MAX_TOTAL - used());
        if (!url) { err.textContent = "That screenshot is too big even made smaller. Crop it to the part of the mail that says so."; break; }
        images.push(url); paint();
      } catch (e) { err.textContent = "That file could not be read as a picture."; }
    }
  }
  f.addEventListener("change", ev => {
    if (ev.target.name === "result" || ev.target.name === "ev" || ev.target.name === "reason") show();
    if (ev.target.id === "cl-file") { add([...ev.target.files]); ev.target.value = ""; }
  });
  f.addEventListener("input", ev => { if (ev.target.name === "text") paint(); });
  // a screenshot pasted anywhere in the form (the text box too) is added as a picture
  f.addEventListener("paste", ev => {
    const files = [...(ev.clipboardData?.items || [])].filter(i => i.kind === "file" && i.type.startsWith("image/")).map(i => i.getAsFile());
    if (files.length) { ev.preventDefault(); if (f.ev.value !== "pasted") { f.ev.value = "pasted"; show(); } add(files); }
  });
  const zone = $("#cl-zone");
  zone.addEventListener("dragover", ev => { ev.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", ev => { ev.preventDefault(); zone.classList.remove("over"); add([...(ev.dataTransfer?.files || [])]); });
  zone.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); $("#cl-file").click(); } });
  f.addEventListener("click", ev => {
    const rm = ev.target.closest("[data-rm]");
    if (rm) { images.splice(Number(rm.dataset.rm), 1); paint(); return; }
    if (ev.target.closest("#cl-pick")) { $("#cl-file").click(); return; }
    if (ev.target.closest("[data-cancel]")) onCancel?.();
  });
  f.onsubmit = async ev => {
    ev.preventDefault(); err.textContent = "";
    const btn = f.querySelector("button[type=submit]"), result = f.result.value, date = f.date.value, evKind = f.ev.value;
    const text = f.text.value.trim();
    if (!result) { err.textContent = "Pick Won or Lost."; return; }
    if (!validIso(date) || date > todayIso()) { err.textContent = "Pick the date it was won or lost, up to today."; return; }
    if (result === "Lost" && !f.reason.value) { err.textContent = "Pick the reason it was lost."; return; }
    const none = result === "Lost" && NO_MAIL.test(f.reason.value);
    if (!none && evKind === "pasted" && !images.length && !text) { err.textContent = "Paste a screenshot or the mail's text, or tick I forwarded it to Iris."; return; }
    if (!none && used() > MAX_TOTAL + MAX_TEXT) { err.textContent = "Too much pasted. Remove a screenshot."; return; }
    btn.disabled = true;
    try {
      const fields = { date, result, reason: result === "Lost" ? f.reason.value : "", rateFrom: result === "Won" ? f.rateFrom.value : "", evidence: none ? "pasted" : evKind };
      await act("close", opp, fields, none ? { evidence: { images: [], text: f.reason.value } }
        : evKind === "pasted" ? { evidence: { images: [...images], text } } : {});
      onDone?.("Submitted.");
    } catch (e) { btn.disabled = false; err.innerHTML = failHtml(e, { part: `${opp} ${result}` }); }
  };
  show(); paint();
  f.querySelector("input[name=result]").focus();
}

// a picture made smaller until it fits what is left of the record: the longest side down to 1,600 px, JPEG
export async function shrink(file, budget) {
  const bmp = await createImageBitmap(file);
  try {
    for (const [side, q] of STEPS) {
      const k = Math.min(1, side / Math.max(bmp.width, bmp.height));
      const w = Math.max(1, Math.round(bmp.width * k)), h = Math.max(1, Math.round(bmp.height * k));
      const cv = document.createElement("canvas"); cv.width = w; cv.height = h;
      const g = cv.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, w, h); g.drawImage(bmp, 0, 0, w, h);
      const url = cv.toDataURL("image/jpeg", q);
      if (url.length <= budget) return url;
    }
    return null;
  } finally { bmp.close?.(); }
}

// the administrator alone can open what is in evidence/
export async function showEvidence(id, pc) {
  const bg = modal(`${pc.result}? ${pc.by ? `from ${pc.by}` : ""}`, `<div class="muted">Loading…</div>`, { wide: true });
  const body = bg.querySelector(".modal-body");
  try {
    const ev = await getEvidence(id);
    if (!ev) { body.innerHTML = `<p class="m0">Nothing was pasted${pc.evidence === "forwarded" ? ": the mail was forwarded to you" : " (or OppList has it already)"}.</p>`; return; }
    body.innerHTML = `${(ev.images || []).map((src, i) => `<img class="ev-img" src="${src}" alt="Screenshot ${i + 1}">`).join("")}
      ${ev.text ? `<pre class="ev-text">${esc(ev.text)}</pre>` : ""}`;
  } catch (e) { body.innerHTML = `<div class="note-box">${failHtml(e, { part: "the pasted mail" })}</div>`; }
}
