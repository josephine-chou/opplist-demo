// Edit Cargo and Route: a drawer
// from the right, like Ask vendors and the quote card, for one cargo line or one route at a time, opened from the case
// page's Cargo and Route panels when OppList lets the site change the case (cases/{OPP}.ed: New or Quoted, ours, not
// legacy) and no Won or Lost from the site is waiting for the administrator. Save writes one edit action: change (the fields
// changed, what the person saw and the new values), add, or strike. OppList takes it in at its next sync; when the same
// thing was changed in OppList in between, that stays and the administrator decides (Suggested updates). Until then the change is
// laid over the Cargo and Route panels for everyone (data.js mergedCase).
// A place is picked from OppList's places (lists/places), else from the official lists of the country chosen
// (official/{CC}.json: UN/LOCODE and IATA airports, static files of the site), else kept as typed (To Confirm in OppList).
import { S, confirmBox } from "../core.js";
import { esc } from "../lib.js";
import { act, getPlaces } from "../data.js";
import { failHtml } from "../issues.js";
import * as E from "../editlib.js";

const OPEN = st => st === "New" || st === "Quoted";
// the case page offers Edit only here (the quote card and Won or Lost wait the same way for a Won or Lost from the site)
export const editable = c => !!(c && c.ed && c.edm && OPEN(c.st) && !c.office && !c.pc && !c.partial);

// The official lists (UN/LOCODE and IATA airports, one file per country) are public data that an installation
// builds and serves beside the site. This copy ships none: OFFICIAL is off, so the picker offers OppList's own
// places and keeps anything else as typed. With the files in official/ and OFFICIAL on, the code below reads
// index.json once and a country's file when it is first searched.
const OFFICIAL = false;
let indexP = null;
const files = new Map();
const officialIndex = () => !OFFICIAL ? Promise.resolve(null)
  : indexP || (indexP = fetch("official/index.json").then(r => r.ok ? r.json() : null).catch(() => null));
function officialFile(cc) {
  if (!OFFICIAL) return Promise.resolve(null);
  if (!files.has(cc)) {
    files.set(cc, fetch(`official/${cc}.json`).then(r => { if (!r.ok) throw new Error(`official/${cc}.json ${r.status}`); return r.json(); })
      .then(E.prepOfficial).catch(e => { files.delete(cc); throw e; }));
  }
  return files.get(cc);
}

export function editPanel(opp, cur, { onDone } = {}) {
  let open = false, dr = null, scrim = null;
  let part = "", target = null;            // the line's or route's id in OppList; null for a new one
  let seen = null;                         // the line or route as the person saw it when the drawer opened
  let picks = { o: null, d: [], pod: [] }, adding = { d: false, pod: false };
  let lists = null, prepped = [], byKey = new Map(), index = null;
  let pk = null, searchT = 0, saving = false;
  const kindsOf = new WeakMap();           // an official pick's possible kinds (not sent)
  const q = id => dr?.querySelector(`#${id}`) || null;
  const listOf = c => (part === "cargo" ? c?.edm?.cargo : c?.edm?.routes) || [];
  const clone = x => JSON.parse(JSON.stringify(x));

  // ------------------------------------------------------------------ opening and closing
  async function show(p, id) {
    hide();
    const c = cur(); if (!editable(c)) return;
    part = p; target = id ?? null;
    const x = target === null ? null : listOf(c).find(r => r.id === target);
    if (target !== null && !x) return;
    seen = x ? clone(x) : null;
    if (seen) { delete seen.pend; delete seen.by; }
    picks = { o: seen?.o ? clone(seen.o) : null, d: clone(seen?.d || []), pod: clone(seen?.pod || []) };
    adding = { d: !picks.d.length, pod: false }; pk = null; saving = false;
    open = true;
    scrim = document.createElement("div"); scrim.className = "ask-scrim";
    dr = document.createElement("aside"); dr.className = "ask-dr ed-dr";
    dr.setAttribute("role", "dialog"); dr.setAttribute("aria-modal", "false"); dr.setAttribute("aria-labelledby", "ed-title");
    const what = part === "cargo" ? (x ? `Cargo line ${x.n}` : "New cargo line") : (x ? `Route ${x.n}` : "New route");
    dr.innerHTML = `<div class="dr-head"><div class="t"><h2 id="ed-title" tabindex="-1">${esc(what)}</h2>
        <div class="dr-sub">${esc(opp)} · ${x ? "change what is wrong, then Save" : "fill in what you know, then Save"}</div></div>
        <button type="button" class="xbtn" data-ed-x aria-label="Close">&times;</button></div>
      <div class="dr-body"><div id="ed-note" role="status"></div><div id="ed-main"><div class="muted">Loading…</div></div>
        <p class="hint small">Saving changes the case for everyone at once. OppList takes it in at its next update (every 5 minutes while Iris's computer is on) and writes it on the Timeline. If she changed the same thing in OppList in the meantime, hers stays and she decides.</p></div>
      <div class="dr-foot"><span class="err" id="ed-err" role="alert"></span>
        ${x && E.canStrike(listOf(c)) ? `<button type="button" class="btn" id="ed-strike">Strike out</button>` : ""}<span class="grow"></span>
        <button type="button" class="btn" data-ed-x>Cancel</button><button type="button" class="btn primary" id="ed-save">${x ? "Save" : part === "cargo" ? "Add the line" : "Add the route"}</button></div>`;
    document.body.append(scrim, dr);
    wire();
    requestAnimationFrame(() => requestAnimationFrame(() => dr?.classList.add("open")));
    document.addEventListener("keydown", onKey, true);
    if (part === "route") {
      try { lists = await getPlaces(); } catch { lists = lists || { rows: [], countries: [] }; }
      prepped = E.prepPlaces(lists.rows); byKey = new Map((lists.rows || []).map(r => [r[0], r]));
      index = await officialIndex();
      if (!open) return;
    }
    paint();
    setTimeout(() => dr?.querySelector("#ed-form input, #ed-form select")?.focus(), 260);
  }
  function hide() {
    if (!open) return;
    open = false; clearTimeout(searchT);
    document.removeEventListener("keydown", onKey, true);
    dr?.remove(); scrim?.remove(); dr = scrim = null; pk = null;
  }
  function close() { hide(); onDone?.("closed"); }
  function onKey(ev) {
    if (ev.key !== "Escape" || !open || document.querySelector(".modal")) return;
    ev.preventDefault();
    if (pk) { closeList(); return; }
    close();
  }

  // ------------------------------------------------------------------ the forms
  const fld = (name, label, control, cls = "", hint = "") => `<label class="ed-f${cls ? " " + cls : ""}" data-f="${name}"><span class="ed-l">${label}</span>${control}${hint ? `<span class="ed-h">${hint}</span>` : ""}<span class="ed-e" id="e-${name}"></span></label>`;
  const inp = (name, value, cls = "", mode = "decimal") => `<input type="text" name="${name}" value="${esc(value)}" class="${cls}" inputmode="${mode}" autocomplete="off">`;
  function sel(name, list, value, blank = "-") {
    const vals = [...(list || [])];
    if (value && !vals.includes(value)) vals.unshift(value);     // a value OppList's lists no longer offer stays as it is
    return `<select name="${name}">${blank === null ? "" : `<option value="">${esc(blank)}</option>`}${vals.map(o => `<option value="${esc(o)}"${o === value ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  }
  const pairs = (name, list, value) => `<select name="${name}">${list.map(([v, l]) => `<option value="${v}"${v === value ? " selected" : ""}>${l}</option>`).join("")}</select>`;
  function cargoHtml(x) {
    const L = S.meta?.editLists || {}, v = k => E.numIn(x?.[k]);
    return `<form id="ed-form" class="ed-form" novalidate autocomplete="off">
      <section class="ed-sec"><h3>What</h3><div class="ed-row">
        ${fld("pieces", "Pieces", inp("pieces", v("pieces"), "w-num", "numeric"))}${fld("pack", "Packing", sel("pack", L.packing, x?.pack || ""))}
        <span class="ed-or">or</span>${fld("cq", "Containers", inp("cq", v("cq"), "w-num", "numeric"))}${fld("ct", "Type", sel("ct", L.containers, x?.ct || ""))}</div></section>
      <section class="ed-sec"><h3>Size of one piece</h3><div class="ed-row">
        ${fld("l", "Length", inp("l", v("l"), "w-num"))}<span class="ed-x" aria-hidden="true">x</span>${fld("w", "Width", inp("w", v("w"), "w-num"))}<span class="ed-x" aria-hidden="true">x</span>${fld("h", "Height", inp("h", v("h"), "w-num"))}
        ${fld("du", "Unit", sel("du", E.DIMS_UNITS, x?.du || ""))}</div></section>
      <section class="ed-sec"><h3>Weight and volume</h3><div class="ed-row">
        ${fld("wt", "Weight", inp("wt", v("wt"), "w-num2"))}${fld("wu", "Unit", sel("wu", E.WEIGHT_UNITS, x?.wu || E.DEFAULTS.wu, null))}${fld("wb", "Counted", pairs("wb", E.WEIGHT_BASES, x?.wb || E.DEFAULTS.wb))}</div>
        <div class="ed-row">${fld("cbm", "Volume (cbm)", inp("cbm", v("cbm"), "w-num2"))}${fld("cb", "Counted", pairs("cb", E.VOLUME_BASES, x?.cb || E.DEFAULTS.cb))}</div></section>
      <section class="ed-sec"><h3>Goods</h3>
        <div class="ed-row">${fld("com", "Commodity", `<input type="text" name="com" value="${esc(x?.com || "")}" maxlength="${E.TEXT_MAX}" autocomplete="off">`, "grow")}</div>
        <div class="ed-row">${fld("hs", "HS code", `<input type="text" name="hs" value="${esc(x?.hs || "")}" class="w-hs" maxlength="${E.TEXT_MAX}" autocomplete="off" spellcheck="false">`, "", "4 to 10 digits each; several with commas")}
          ${fld("val", "Cargo value", inp("val", v("val"), "w-num2"))}${fld("cur", "Currency", `<input type="text" name="cur" value="${esc(x?.cur || "")}" class="w-cur" maxlength="3" placeholder="USD" autocomplete="off" spellcheck="false">`)}</div></section>
    </form>`;
  }
  function routeHtml(x) {
    const L = S.meta?.editLists || {}, cn = (lists?.countries || []).map(c => c[0]);
    return `<form id="ed-form" class="ed-form" novalidate autocomplete="off">
      <section class="ed-sec"><h3>From</h3><div class="ed-row">${fld("oc", "Country", sel("oc", cn, x?.oc || ""))}</div><div class="pp-slot" data-slot="o"></div></section>
      <section class="ed-sec"><h3>To</h3><div class="ed-row">${fld("dc", "Country", sel("dc", cn, x?.dc || ""))}</div><div class="pp-slot" data-slot="d"></div></section>
      <section class="ed-sec"><h3>Port of discharge</h3><div class="pp-slot" data-slot="pod"></div></section>
      <section class="ed-sec"><h3>Service</h3><div class="ed-row">${fld("mode", "Mode", sel("mode", L.modes, x?.mode || ""))}${fld("inc", "Incoterm", sel("inc", L.incoterms, x?.inc || ""))}${fld("scope", "Service", sel("scope", L.scopes, x?.scope || ""))}</div></section>
    </form>`;
  }
  function paint() {
    if (!dr) return;
    q("ed-main").innerHTML = part === "cargo" ? cargoHtml(seen) : routeHtml(seen);
    if (part === "route") for (const s of E.PLACE_FIELDS) paintSlot(s);
    refresh();
  }

  // ------------------------------------------------------------------ the place picker
  const LAB = { o: "Origin", d: "Destination", pod: "Port of discharge" };
  const countryOf = slot => dr?.querySelector(`#ed-form [name="${slot === "o" ? "oc" : "dc"}"]`)?.value || "";
  const isoOf = name => (lists?.countries || []).find(c => c[0] === name)?.[1] || "";
  function paintSlot(slot) {
    const box = dr?.querySelector(`.pp-slot[data-slot="${slot}"]`); if (!box) return;
    const vals = slot === "o" ? (picks.o ? [picks.o] : []) : picks[slot];
    let html = vals.map((p, i) => chipHtml(slot, i, p)).join("");
    if (slot === "d" && vals.length > 1) html += `<p class="hint small">The first is the route's destination.</p>`;
    const room = slot === "o" ? !vals.length : vals.length < E.MAX_PLACES;
    if (room && (slot === "o" || adding[slot])) html += pickerHtml(slot, vals.length ? `Another ${LAB[slot].toLowerCase()}` : LAB[slot]);
    else if (room) html += `<button type="button" class="linkbtn small pp-add" data-pp-add="${slot}">+ ${vals.length ? `Another ${LAB[slot].toLowerCase()}` : LAB[slot]}</button>`;
    html += `<span class="ed-e" id="e-${slot}"></span>`;
    box.innerHTML = html;
  }
  function chipHtml(slot, i, p) {
    const r = p.k ? byKey.get(p.k) : null;
    const main = p.k || (p.off ? p.off.name : p.t);
    const sub = p.k ? (r ? [r[1] && r[1].toLowerCase() !== p.k.toLowerCase() ? r[1] : "", r[3], r[2]].filter(Boolean).join(" · ") : "in OppList")
      : p.off ? [E.offCode(p.off), p.off.sub, "official list, new in OppList"].filter(Boolean).join(" · ")
      : "as typed · To Confirm in OppList";
    const kinds = p.off ? kindsOf.get(p) || [p.off.kind] : [];
    const kind = kinds.length > 1 ? `<select class="sel-sm" data-kind="${slot}:${i}" aria-label="What ${esc(main)} is">${kinds.map(k => `<option${k === p.off.kind ? " selected" : ""}>${k}</option>`).join("")}</select>`
      : p.off ? `<span class="chip">${esc(p.off.kind)}</span>` : "";
    return `<div class="pp-chip" data-chip="${slot}:${i}"><span class="pp-main">${esc(main)}</span><span class="muted small">${esc(sub)}</span>${kind}
      <button type="button" class="linkbtn small" data-pp-x="${slot}:${i}">${slot === "o" ? "Change" : "Remove"}</button></div>`;
  }
  const pickerHtml = (slot, label, value = "") => `<div class="pp" data-pp="${slot}"><label class="ed-l" for="pp-in-${slot}">${esc(label)}</label>
      <input type="text" id="pp-in-${slot}" class="pp-in" value="${esc(value)}" placeholder="Type a city, port, airport or code" role="combobox" aria-autocomplete="list"
        aria-expanded="false" aria-controls="pp-ls-${slot}" autocomplete="off" spellcheck="false" maxlength="${E.TEXT_MAX}">
      <ul class="pp-list" id="pp-ls-${slot}" role="listbox" aria-label="${esc(label)}" hidden></ul></div>`;
  function schedule(slot, typed) {
    clearTimeout(searchT);
    if (!typed.trim()) { closeList(); return; }
    pk = { slot, typed, items: [], active: 0 };
    searchT = setTimeout(() => search(slot, typed), 90);
  }
  async function search(slot, typed) {
    const country = countryOf(slot), mode = dr?.querySelector("#ed-form [name=mode]")?.value || "", iso = isoOf(country);
    let file = null, note = "";
    if (!country) note = "Pick the country above to search the official lists too.";
    else if (!iso || !index?.countries?.[iso]) note = index ? `The official lists have nothing for ${country}.` : "";
    else { try { file = await officialFile(iso); } catch { note = "The official list could not be loaded; try again in a moment."; } }
    if (!open || pk?.slot !== slot || pk.typed !== typed) return;          // something newer was typed
    const res = E.pickList({ prepped, file, typed, country, mode });
    pk.items = [...res.places.map(r => ({ kind: "p", r })), ...res.official.map(e => ({ kind: "o", e })), { kind: "t" }];
    pk.active = 0;
    const ul = q(`pp-ls-${slot}`), input = q(`pp-in-${slot}`); if (!ul || !input) return;
    let i = 0;
    const li = html => { const k = i++; return `<li role="option" id="pp-o-${slot}-${k}" data-i="${k}" class="pp-opt${k === 0 ? " on" : ""}" aria-selected="${k === 0}">${html}</li>`; };
    ul.innerHTML = (res.places.length ? `<li class="pp-h" role="presentation">In OppList</li>` : "")
      + res.places.map(r => li(`<b>${esc(r[0])}</b> <span class="muted">${esc([r[1] && r[1].toLowerCase() !== String(r[0]).toLowerCase() ? r[1] : "", r[3], r[2], r[4]].filter(Boolean).join(" · "))}</span>`)).join("")
      + (res.official.length ? `<li class="pp-h" role="presentation">Official list · ${esc(country)}</li>` : "")
      + res.official.map(e => li(`<b>${esc(e.name)}</b> <span class="muted">${esc([E.offCode(e), e.sub, e.kind].filter(Boolean).join(" · "))}</span>`)).join("")
      + (note ? `<li class="pp-h pp-note" role="presentation">${esc(note)}</li>` : "")
      + li(`Keep “${esc(typed.trim())}” as typed <span class="muted">· To Confirm in OppList</span>`);
    ul.hidden = false; input.setAttribute("aria-expanded", "true"); input.setAttribute("aria-activedescendant", `pp-o-${slot}-0`);
  }
  function closeList() {
    clearTimeout(searchT);
    if (pk) { const ul = q(`pp-ls-${pk.slot}`), input = q(`pp-in-${pk.slot}`); if (ul) { ul.hidden = true; ul.innerHTML = ""; } input?.setAttribute("aria-expanded", "false"); input?.removeAttribute("aria-activedescendant"); }
    pk = null;
  }
  function move(by) {
    if (!pk?.items.length) return;
    pk.active = (pk.active + by + pk.items.length) % pk.items.length;
    dr.querySelectorAll(`#pp-ls-${pk.slot} .pp-opt`).forEach(el => { const on = Number(el.dataset.i) === pk.active; el.classList.toggle("on", on); el.setAttribute("aria-selected", on); if (on) el.scrollIntoView({ block: "nearest" }); });
    q(`pp-in-${pk.slot}`)?.setAttribute("aria-activedescendant", `pp-o-${pk.slot}-${pk.active}`);
  }
  function choose(i) {
    if (!pk) return;
    const { slot, typed } = pk, item = pk.items[i]; if (!item) return;
    let value;
    if (item.kind === "p") {
      value = { k: item.r[0] };
      const box = dr.querySelector(`#ed-form [name="${slot === "o" ? "oc" : "dc"}"]`);   // the country, when none is picked yet
      if (box && !box.value && [...box.options].some(o => o.value === item.r[2])) box.value = item.r[2];
    } else if (item.kind === "o") { value = E.offPick(item.e, typed); kindsOf.set(value, item.e.kinds); }
    else { const t = typed.trim(); if (!t) return; value = { t }; }
    closeList();
    if (slot === "o") picks.o = value; else { picks[slot].push(value); adding[slot] = false; }
    paintSlot(slot);
    if (!dr.querySelector("[aria-invalid]")) q("ed-err").textContent = "";
    const n = slot === "o" ? 0 : picks[slot].length - 1;
    (dr.querySelector(`[data-pp-x="${slot}:${n}"]`) || q("ed-save"))?.focus();
  }

  // ------------------------------------------------------------------ the events
  function wire() {
    dr.addEventListener("click", ev => {
      if (ev.target.closest("[data-ed-x]")) { close(); return; }
      if (ev.target.closest("#ed-save")) { save(); return; }
      if (ev.target.closest("#ed-strike")) { strike(); return; }
      if (ev.target.closest("[data-ed-again]")) { const p = part, t = target; show(p, t); return; }
      const add = ev.target.closest("[data-pp-add]");
      if (add) { adding[add.dataset.ppAdd] = true; paintSlot(add.dataset.ppAdd); q(`pp-in-${add.dataset.ppAdd}`)?.focus(); return; }
      const x = ev.target.closest("[data-pp-x]");
      if (x) {
        const [slot, i] = x.dataset.ppX.split(":");
        if (slot === "o") { const was = E.placeName(picks.o) || ""; picks.o = null; paintSlot("o"); const input = q("pp-in-o"); if (input) { input.value = was; input.focus(); input.select(); } }
        else { picks[slot].splice(Number(i), 1); paintSlot(slot); }
      }
    });
    dr.addEventListener("mousedown", ev => {
      const opt = ev.target.closest(".pp-opt"); if (!opt) return;
      ev.preventDefault(); choose(Number(opt.dataset.i));
    });
    dr.addEventListener("input", ev => {
      const input = ev.target.closest(".pp-in"); if (input) { schedule(input.closest("[data-pp]").dataset.pp, input.value); return; }
      const f = ev.target.closest("[data-f]"); if (f) { const e = q(`e-${f.dataset.f}`); if (e) e.textContent = ""; ev.target.removeAttribute("aria-invalid"); }
      if (!dr.querySelector("[aria-invalid]")) q("ed-err").textContent = "";       // the last box put right: the word at the foot goes too
    });
    dr.addEventListener("keydown", ev => {
      const input = ev.target.closest(".pp-in"); if (!input) return;
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!pk) { schedule(input.closest("[data-pp]").dataset.pp, input.value); return; }
        move(ev.key === "ArrowDown" ? 1 : -1);
      } else if (ev.key === "Enter") { ev.preventDefault(); if (pk?.items.length) choose(pk.active); }
    });
    dr.addEventListener("focusout", ev => {
      if (ev.target.closest?.(".pp-in")) setTimeout(() => { if (pk && !dr?.querySelector(".pp-in:focus")) closeList(); }, 150);
    });
    dr.addEventListener("change", ev => {
      const k = ev.target.closest("[data-kind]");
      if (k) { const [slot, i] = k.dataset.kind.split(":"); const p = slot === "o" ? picks.o : picks[slot][Number(i)]; if (p?.off) p.off.kind = k.value; return; }
      // the country chosen changes what the open picker finds
      if (pk && ["oc", "dc", "mode"].includes(ev.target.name)) search(pk.slot, pk.typed);
    });
  }

  // ------------------------------------------------------------------ saving
  function showErrs(errs) {
    let first = null;
    for (const [k, msg] of Object.entries(errs)) {
      if (k === "_") continue;
      const e = q(`e-${k}`); if (e) e.textContent = msg;
      const box = dr.querySelector(`#ed-form [name="${k}"]`) || q(`pp-in-${k}`);
      if (box) { box.setAttribute("aria-invalid", "true"); first = first || box; }
    }
    q("ed-err").textContent = errs._ || "Check the boxes marked in red.";
    first?.focus();
  }
  function clearErrs() {
    dr.querySelectorAll(".ed-e").forEach(e => { e.textContent = ""; });
    dr.querySelectorAll("[aria-invalid]").forEach(e => e.removeAttribute("aria-invalid"));
    q("ed-err").textContent = "";
  }
  async function save() {
    if (saving || !dr) return;
    const f = q("ed-form"); if (!f) return;
    clearErrs();
    if (!editable(cur())) { refresh(); return; }
    let vals, errs;
    if (part === "cargo") {
      ({ vals, errs } = E.readCargo(Object.fromEntries(E.CARGO_FIELDS.map(k => [k, f.elements[k]?.value ?? ""]))));
      if (!Object.keys(errs).length) errs = E.checkCargo(vals, seen);
    } else {
      ({ vals, errs } = E.readRoute(Object.fromEntries(["oc", "dc", "mode", "inc", "scope"].map(k => [k, f.elements[k]?.value ?? ""])), picks));
      // a place typed and not picked: the person says what it is (one of the list, or Keep it as typed)
      for (const s of E.PLACE_FIELDS) if (q(`pp-in-${s}`)?.value.trim()) errs[s] = "Pick one from the list, or Keep it as typed";
      if (!Object.keys(errs).length) errs = E.checkRoute(vals, seen);
    }
    if (Object.keys(errs).length) { showErrs(errs); return; }
    let fields;
    if (target === null) fields = { part, op: "add", to: E.addValues(vals, part) };
    else {
      const { from, to, n } = E.changes(seen, vals, part === "cargo" ? E.CARGO_FIELDS : E.ROUTE_FIELDS);
      if (!n) { q("ed-err").textContent = "Nothing is changed yet."; return; }
      fields = { part, op: "change", id: target, from, to };
    }
    const btn = q("ed-save"); saving = true; btn.disabled = true;
    try {
      await act("edit", opp, fields);
      hide();
      onDone?.("saved", target === null ? `${part === "cargo" ? "Cargo line" : "Route"} added. OppList takes it in at its next update.`
        : `${part === "cargo" ? `Cargo line ${seen.n}` : `Route ${seen.n}`} changed. OppList takes it in at its next update.`);
    } catch (e) { saving = false; if (btn) btn.disabled = false; const err = q("ed-err"); if (err) err.innerHTML = failHtml(e, { part: opp }); }
  }
  async function strike() {
    const thing = part === "cargo" ? `cargo line ${seen?.n}` : `route ${seen?.n}`;
    if (!await confirmBox(`Strike out ${thing}?`, `OppList strikes it out at its next update. It stays on the case there, struck out; nothing is deleted.`, "Strike out")) return;
    if (!open || !editable(cur())) return;
    try { await act("edit", opp, { part, op: "strike", id: target }); hide(); onDone?.("saved", `${thing[0].toUpperCase()}${thing.slice(1)} struck out. OppList takes it in at its next update.`); }
    catch (e) { const err = q("ed-err"); if (err) err.innerHTML = failHtml(e, { part: opp }); }
  }

  // ------------------------------------------------------------------ the case's live updates
  // the form is never redrawn under the person: a note says what changed meanwhile, and Save stops when the case or the
  // line is no longer one the site may change
  function refresh() {
    if (!open || !dr) return;
    const c = cur(), note = q("ed-note"), save = q("ed-save");
    const live = target === null ? null : listOf(c).find(r => r.id === target);
    let msg = "", again = false, stop = false;
    if (!editable(c)) { msg = c?.pc ? "A Won or Lost from the site is waiting for Iris, so the case cannot be changed here now. Nothing was saved." : "OppList no longer lets the site change this case (it was closed, or another office has it). Nothing was saved."; stop = true; }
    else if (target !== null && !live) { msg = `This ${part === "cargo" ? "line" : "route"} is no longer on the case. Nothing was saved.`; stop = true; }
    else if (live) {
      const moved = (part === "cargo" ? E.CARGO_FIELDS : E.ROUTE_FIELDS).filter(f => !E.sameField(f, seen?.[f], live[f]));
      if (moved.length) { msg = `Changed while this was open: ${moved.map(f => `${E.LABELS[f]} is now ${E.show(live[f])}`).join("; ")}. If you change ${moved.length === 1 ? "it" : "them"} too, Iris decides which stays.`; again = true; }
    }
    if (note) note.innerHTML = msg ? `<div class="note-box small">${esc(msg)}${again ? ` <button type="button" class="linkbtn small" data-ed-again>Start again from these values</button>` : ""}</div>` : "";
    if (save) save.disabled = stop || saving;
    const st = q("ed-strike"); if (st) st.disabled = stop || !E.canStrike(listOf(c));
  }

  return {
    isOpen: () => open,
    current: () => open ? { part, id: target } : null,
    show, refresh,
    leave() { hide(); },
  };
}
