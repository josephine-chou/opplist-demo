// A vendor's rates for this shipment: a drawer from the right, like Ask vendors and the quote card, opened from a vendor's
// Rate requests row. The lines as the vendor quoted them (part, charge, unit, rate, minimum; the quantity from the
// case's cargo and what each line comes to, unless typed), the currency, valid until, transit, carrier, free time,
// three questions (fuel, local charges, minimum: in the rates, on top, none, or not said), a note, the
// route (a case with several), and the day the vendor sent them (their day decides whether it is a
// new quote or a correction of the last one). Save writes vendorRates/{id} with a rates action of
// the same id; every save is a version and the old ones stay (OppList keeps them all). Everyone on the site sees them.
// A closed case's rates are shown read only.
// Opened from a Read quote draft (a vendor's reply read with Claude on the computer OppList runs on), the drawer
// starts from the draft's option, says where it was read from and what to look at, shows under each line what the
// vendor wrote, switches between the vendor's options, and saves the version with the draft's id, the option and
// how "read"; Set aside writes a discard action.
// Once OppList keeps options (meta/sync.options), a vendor can have several options on a route, each with its
// name. From a reading, every option
// is saved or skipped in turn (Save this option moves to the next, Skip this option writes a discard with the option);
// typing by hand, Their options opens each one saved and + Another option starts a new one, which needs a name.
import { S, confirmBox } from "../core.js";
import { esc, day, todayIso, validIso } from "../lib.js";
import { act } from "../data.js";
import { failHtml } from "../issues.js";
import * as Q from "../quotemail.js";
import * as R from "../ratelib.js";
import { letter } from "../editlib.js";

const OPEN = st => st === "New" || st === "Quoted";
// who may type rates: a New or Quoted case of ours, once it is loaded (OppList takes a closed case's rates in too, but
// the site keeps to the open ones)
export const canRates = c => !!(c && OPEN(c.st) && !c.office && !c.partial);

export function ratesPanel(opp, cur, { rates = () => ({ best: new Map(), docs: [] }), onDone } = {}) {
  let open = false, dr = null, scrim = null, card = null, ro = false, tried = false, saving = false, dirty = false, prev = null, src = null;
  const optsOn = () => S.meta?.options === true;
  const txt = v => String(v ?? "").trim();
  const q = id => dr?.querySelector(`#${id}`) || null;
  const mode = () => R.modeFor(cur() || {}, card?.route ?? null);
  const autoFn = unit => R.autoQty(cur() || {}, unit, mode());

  // ------------------------------------------------------------------ the card
  // the vendor's newest rates for the route (to start from), else the case's freight
  function cardFor(c, vendor, route, label = "", isNew = false) {
    const got = isNew ? null : rates().best.get(R.keyOf(vendor, route, label));
    prev = got || null;
    // an option saved keeps its name (fixed); a new one is named here
    const lab = !optsOn() ? {} : got ? { label: txt(got.doc.label), fixed: true } : { label: "", isNew };
    const replied = (c?.rr || []).filter(x => x.vendor === vendor && x.replied).map(x => x.replied).sort().slice(-1)[0] || "";
    const today = todayIso();
    if (got) {
      const d = got.doc, m = R.modeFor(c, route);
      return { vendor, route, ...lab, on: d.on || today, cur: d.cur || "USD", lines: R.linesFromDoc(d, u => R.autoQty(c, u, m)), valid: d.valid || "",
        transit: d.transit || "", carrier: d.carrier || "", free: d.free || "", said: { ...{ fuel: "notsaid", local: "notsaid", min: "notsaid" }, ...(d.said || {}) }, note: d.note || "" };
    }
    return { vendor, route, ...lab, on: replied && replied <= today ? replied : today, cur: "USD", lines: R.startLines(c, R.modeFor(c, route)), valid: "",
      transit: "", carrier: "", free: "", said: { fuel: "notsaid", local: "notsaid", min: "notsaid" }, note: "" };
  }

  // a Read quote draft's option (src: {draft, option}); the vendor's rates saved before stay as older versions
  function cardFromDraft(c, vendor, draft, i) {
    const route = R.routesOf(c).length ? (typeof draft.route === "number" && R.routeOf(c, draft.route) ? draft.route : R.defaultRoute(c)) : null;
    const m = R.modeFor(c, route), d = R.draftCard(draft, i, u => R.autoQty(c, u, m)), today = todayIso();
    if (!optsOn()) delete d.label;
    prev = rates().best.get(R.keyOf(vendor, route, d.label || "")) || null;
    return { vendor, route, on: validIso(draft.on) && draft.on <= today ? draft.on : today, ...d };
  }

  // ------------------------------------------------------------------ opening and closing
  function show(vendor, { route, draft, label = "", add = false } = {}) {
    hide();
    const c = cur(); if (!c || !vendor) return;
    ro = !canRates(c);
    // what has become of each of the draft's options (OppList's taken and what was done here)
    const taken = draft && optsOn() ? R.takenOf(draft, rates().docs, rates().discards) : [];
    const first = Math.max(0, taken.findIndex(t => !t));
    src = draft ? { draft, option: first, taken, step: "" } : null;
    card = src ? cardFromDraft(c, vendor, draft, first) : cardFor(c, vendor, R.routesOf(c).length ? route ?? R.defaultRoute(c) : null, label, add);
    tried = false; saving = false; dirty = false; open = true;
    scrim = document.createElement("div"); scrim.className = "ask-scrim";
    dr = document.createElement("aside"); dr.className = "ask-dr rt-dr";
    dr.setAttribute("role", "dialog"); dr.setAttribute("aria-modal", "false"); dr.setAttribute("aria-labelledby", "rt-title");
    dr.innerHTML = `<div class="dr-head"><div class="t"><h2 id="rt-title" tabindex="-1"></h2><div class="dr-sub"></div></div>
        <button type="button" class="xbtn" data-rt-x aria-label="Close">&times;</button></div>
      <div class="dr-body"></div>
      <div class="dr-foot"></div>
      <datalist id="rt-dl-charge">${Q.CHARGES.map(x => `<option value="${esc(x)}">`).join("")}</datalist>
      <datalist id="rt-dl-unit">${R.UNITS.map(x => `<option value="${esc(x)}">`).join("")}</datalist>
      <datalist id="rt-dl-rate">${R.WORDS.map(x => `<option value="${esc(x)}">`).join("")}</datalist>`;
    document.body.append(scrim, dr);
    wire(); paint();
    requestAnimationFrame(() => requestAnimationFrame(() => dr?.classList.add("open")));
    document.addEventListener("keydown", onKey, true);
    setTimeout(() => q("rt-title")?.focus(), 260);
  }
  function hide() {
    if (!open) return;
    open = false;
    document.removeEventListener("keydown", onKey, true);
    dr?.remove(); scrim?.remove(); dr = scrim = null; card = null; prev = null; src = null;
  }
  async function close(force = false) {
    if (!force && dirty && !ro && !await confirmBox("Close the rates", "Close without saving? What you typed here is not kept.", "Close")) return;
    hide(); onDone?.("closed");
  }
  function onKey(ev) {
    if (ev.key !== "Escape" || !open || document.querySelector(".modal")) return;
    // stopped here, or the confirm box close() opens would take this Escape too and close at once
    ev.preventDefault(); ev.stopPropagation(); close();
  }

  // ------------------------------------------------------------------ drawing
  function paint() {
    if (!dr || !card) return;
    const c = cur() || {};
    q("rt-title").textContent = `Rates from ${card.vendor}`;
    const rt = R.routeOf(c, card.route);
    dr.querySelector(".dr-sub").textContent = [opp, rt ? `route ${letter(rt.n)}` : "", mode(), ends(c, rt), Q.cargoPhrase(c), rt?.incoterm || Q.incotermOf(c)].filter(Boolean).join(" · ");
    dr.querySelector(".dr-body").innerHTML = bodyHtml(c);
    paintFoot();
  }
  const ends = (c, rt) => { const e = rt || (c.routes || [])[0]; return e && (e.from || e.to) ? `${Q.placeName(e.from)} to ${Q.placeName(e.to)}` : c.lane || ""; };
  function paintFoot() {
    const foot = dr.querySelector(".dr-foot");
    if (ro) {
      const c = cur() || {}, why = c.office ? `${c.office} handles this case` : c.partial ? "The case is still loading" : `This case is ${c.st}`;
      foot.innerHTML = `<span class="info">${esc(why)}: its rates are shown, not changed.</span><span class="grow"></span><button type="button" class="btn primary" data-rt-x>Close</button>`; return;
    }
    const several = !!(src && optsOn() && (src.draft.options || []).length > 1);
    foot.innerHTML = `<span class="err" id="rt-err" role="alert"></span><span class="info">Everyone on the site sees them; the quote card can use them.</span>`
      + `<span class="grow"></span><button type="button" class="btn" data-rt-x>Cancel</button>`
      + (several ? `<button type="button" class="btn" id="rt-skip">Skip this option</button><button type="button" class="btn primary" id="rt-save">Save this option</button>`
        : `<button type="button" class="btn primary" id="rt-save">Save rates</button>`);
  }
  function bodyHtml(c) {
    const today = todayIso(), rs = R.routesOf(c), dis = ro ? " disabled" : "";
    const T = R.totalOf(card.lines, autoFn), probs = tried ? R.problems(card, { today, existing: existing() }) : [];
    const pv = prev?.doc;
    return `${probs.length ? `<div class="problems" role="status"><b>Before you save</b><ul>${probs.map(p => `<li>${esc(p)}</li>`).join("")}</ul></div>` : ""}
      ${src ? readHtml() : optionsHtml(dis)}
      <div class="qtop rt-top">
        ${optsOn() && !card.fixed ? `<label class="fld-in">Option name${card.isNew || src ? "" : " (optional)"} <input type="text" id="rt-label" value="${esc(card.label || "")}" maxlength="${R.LABEL_MAX}" placeholder="e.g. Truckload, MAERSK SPOT"${dis}${tried && R.problems(card, { existing: existing() }).some(x => /option/.test(x)) ? ' aria-invalid="true"' : ""}></label>` : ""}
        ${rs.length > 1 ? `<label class="fld-in">Route <select id="rt-route"${dis}>${rs.map(r => `<option value="${r.n}"${r.n === card.route ? " selected" : ""}>${esc(`${letter(r.n)} · ${[r.mode, `${Q.placeName(r.from)} to ${Q.placeName(r.to)}`].filter(Boolean).join(" · ")}`)}</option>`).join("")}</select></label>` : ""}
        <label class="fld-in">They sent them on <input type="date" id="rt-on" value="${esc(card.on)}" max="${today}"${dis}${tried && (!card.on || card.on > today) ? ' aria-invalid="true"' : ""}></label>
        <label class="fld-in">Currency <select id="rt-cur"${dis}${tried && !card.cur ? ' aria-invalid="true"' : ""}>${card.cur ? "" : '<option value="" selected>Pick</option>'}${Q.CURRENCIES.map(x => `<option${x === card.cur ? " selected" : ""}>${x}</option>`).join("")}</select></label></div>
      <p class="muted small m0" id="rt-prev">${src ? `From the reading above.${pv ? ` Their rates as ${esc(pv.name || "someone")} saved them on ${esc(day(stampDay(pv.at), true))} stay as an older version.` : ""} The quantity comes from the case's cargo unless the vendor wrote one.`
        : pv ? `Their rates as ${esc(pv.name || "someone")} saved them on ${esc(day(stampDay(pv.at), true))}${prev.older ? ` (${prev.older} older version${prev.older === 1 ? "" : "s"} kept)` : ""}. ${ro ? "" : "Saving again keeps these too. If they sent new rates, change the day they sent them; the same day is a correction."}`
        : `Type the rates as ${esc(card.vendor)} quoted them. The quantity comes from the case's cargo and each line works out what it comes to; type a quantity or an amount to set it yourself.`}</p>
      <div class="rt-lines"><div class="rh" aria-hidden="true"><span>Part</span><span>Charge</span><span>Unit</span><span class="r">Rate</span><span class="r">Min</span><span class="r">Qty</span><span class="r">Amount</span><span></span></div>
        <div id="rt-ls">${card.lines.map((l, i) => lineHtml(l, i, dis)).join("")}</div></div>
      ${ro ? "" : `<div class="qadd"><button type="button" class="btn small" id="rt-add">+ Add a line</button><span class="lbl">The parts:</span><span class="muted small">Freight, Inland (IPI, the port to an inland point), Origin, Destination, Customs, Other</span></div>`}
      <div class="qtot"><span class="muted">Their total for this shipment</span><span class="v" id="rt-total">${T.any ? `${esc(card.cur)} ${Q.money(T.t)}` : "-"}</span><span class="muted small" id="rt-atcost">${T.atCost ? "plus the AT COST lines" : ""}</span></div>
      <section class="qc-sec" aria-labelledby="rt-h-v"><h3 id="rt-h-v">Validity and terms</h3>
        <div class="row2">
          <label class="field">Valid until<input type="date" id="rt-valid" value="${esc(card.valid)}"${dis}${tried && card.valid && !/^\d{4}-\d{2}-\d{2}$/.test(card.valid) ? ' aria-invalid="true"' : ""}></label>
          <label class="field">Transit time<input type="text" id="rt-transit" value="${esc(card.transit)}" maxlength="${R.SHORT_MAX}" placeholder="as they wrote it, e.g. 30 to 35 days"${dis}></label></div>
        <div class="row2">
          <label class="field">Carrier<input type="text" id="rt-carrier" value="${esc(card.carrier)}" maxlength="${R.SHORT_MAX}" placeholder="e.g. CMA CGM, Lufthansa"${dis}></label>
          <label class="field">Free time<input type="text" id="rt-free" value="${esc(card.free)}" maxlength="${R.SHORT_MAX}" placeholder="e.g. 14 days at destination"${dis}></label></div></section>
      <section class="qc-sec" aria-labelledby="rt-h-s"><h3 id="rt-h-s">What they said <span>the three things to ask</span></h3>
        ${R.SAID.map(([k, label]) => `<div class="said-row" role="radiogroup" aria-label="${esc(label)}"><span class="k">${esc(label)}</span>${R.SAID_VALUES.map(([v, t]) =>
          `<label class="seg${card.said[k] === v ? " on" : ""}${v === "notsaid" ? " ns" : ""}"><input type="radio" name="rt-said-${k}" value="${v}" data-said="${k}"${card.said[k] === v ? " checked" : ""}${dis}> ${esc(t)}</label>`).join("")}</div>`).join("")}</section>
      <label class="field">Note (optional)<textarea id="rt-note" rows="2" maxlength="${R.NOTE_MAX}" placeholder="e.g. subject to space; rates from their mail of Nov 12"${dis}>${esc(card.note)}</textarea></label>`;
  }
  function lineHtml(l, i, dis) {
    const auto = autoFn(l.unit), a = R.amountOf(l, auto), miss = tried && R.used([l]).length && !a;
    const qtyPh = auto === null ? "1" : String(auto), amtPh = a && !a.hand ? (a.n !== undefined ? Q.money(a.n) : a.w) : "";
    return `<div class="rl${miss ? " miss" : ""}" data-i="${i}">
      <div class="sg" data-l="Part"><select data-f="seg" aria-label="Part"${dis}>${R.SEGS.map(x => `<option${x === l.seg ? " selected" : ""}>${x}</option>`).join("")}</select></div>
      <div class="ch" data-l="Charge"><input type="text" list="rt-dl-charge" aria-label="Charge" data-f="charge" value="${esc(l.charge)}" maxlength="${R.TEXT_MAX}" placeholder="e.g. OCEAN FREIGHT"${dis}></div>
      <div class="un${l.asWritten && !l.unit ? " aw" : ""}" data-l="Unit"><input type="text" list="rt-dl-unit" aria-label="Unit" data-f="unit" value="${esc(l.unit)}" maxlength="40" placeholder="${esc(l.asWritten && !l.unit ? l.asWritten : "per")}"${l.asWritten && !l.unit ? ` title="${esc(`The vendor wrote ${l.asWritten}: pick a unit of the list, or leave it empty (it counts once)`)}"` : ""}${dis}></div>
      <div class="r rt" data-l="Rate"><input type="text" list="rt-dl-rate" inputmode="decimal" aria-label="Rate" data-f="rate" value="${esc(l.rate)}" maxlength="20"${dis}></div>
      <div class="r mn" data-l="Min"><input type="text" inputmode="decimal" aria-label="Minimum" data-f="min" value="${esc(l.min)}" maxlength="20"${dis}></div>
      <div class="r q" data-l="Qty"><input type="text" inputmode="decimal" aria-label="Quantity" data-f="qty" value="${esc(l.qty)}" maxlength="20" placeholder="${esc(qtyPh)}" title="From the case's cargo; type one to set it yourself"${dis}></div>
      <div class="r amt" data-l="Amount"><input type="text" inputmode="decimal" aria-label="Amount" data-f="amount" value="${esc(l.amount)}" maxlength="20" placeholder="${esc(amtPh)}" title="Rate x Qty, or the minimum when it is more. Type an amount to set it yourself."${dis}></div>
      <div class="xcell">${ro ? "" : `<button type="button" class="x" data-rm="${i}" aria-label="Remove this line" title="Remove this line">&times;</button>`}</div>
      ${a?.min ? '<div class="more"><span class="hand">The minimum applies</span></div>' : a?.hand ? '<div class="more"><span class="hand">Amount set by hand</span></div>' : ""}
      ${l.quote ? `<div class="rq-q"><span class="muted">They wrote:</span> ${esc(l.quote)}${l.asWritten && !l.unit ? ` <span class="aw-note">· their unit, ${esc(l.asWritten)}, is not one of the list: pick one, or leave it empty (it counts once)</span>` : ""}</div>` : ""}
    </div>`;
  }
  // the names of the vendor's options on this card's route
  const existing = () => optsOn() && card ? R.optionsOf(rates().best, card.vendor, card.route).map(o => txt(o.doc.label)) : [];
  // typing by hand, the vendor's options on the route (each opens; + Another option starts a new one)
  function optionsHtml(dis) {
    if (!optsOn()) return "";
    const list = R.optionsOf(rates().best, card.vendor, card.route);
    if (!list.length) return "";
    // one set with no name: only the way to another
    if (list.length === 1 && !txt(list[0].doc.label) && !card.isNew) return ro ? "" : `<p class="m0 rt-another"><button type="button" class="linkbtn small" data-rt-optadd>+ Another option</button> <span class="muted small">when they quoted another too (a truckload and a partial, another carrier)</span></p>`;
    const on = card.fixed ? R.labelKey(card.label) : null;
    return `<div class="said-row rt-opts rt-mine" role="group" aria-label="Their options"><span class="k">Their options</span>${list.map(o => {
        const k = R.labelKey(o.doc.label);
        return `<button type="button" class="seg${on === k ? " on" : ""}" data-rt-optkey="${esc(txt(o.doc.label))}" aria-pressed="${on === k}">${esc(txt(o.doc.label) || "Unnamed")}</button>`;
      }).join("")}${ro ? "" : `<button type="button" class="seg${card.isNew ? " on" : ""}" data-rt-optadd aria-pressed="${!!card.isNew}">+ Another option</button>`}</div>`;
  }
  // where the reading came from, what to look at, the vendor's options, Set aside
  function readHtml() {
    const d = src.draft, opts = d.options || [], h = R.draftFrom(d);
    // the mail's subject and when it was sent, nothing more
    return `<div class="rt-read" role="note" id="rt-read"><p class="m0"><b>${esc(h.what)}</b>${h.when ? `<span class="muted small"> · ${esc(h.when)}</span>` : ""}</p>
      <p class="m0 small">Check every line against the mail before you save: what the vendor wrote is under each line. Nothing is kept as their rates until you press Save.</p>
      ${(d.warnings || []).length ? `<ul class="rt-warn">${d.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      ${opts.length > 1 ? `<div class="said-row rt-opts" role="radiogroup" aria-label="The vendor's options"><span class="k">Their options</span>${opts.map((o, i) => {
        // an option saved or skipped is marked and cannot be opened from the reading again
        const t = src.taken?.[i] || "";
        return t ? `<span class="seg done">${t === "saved" ? "✓ " : ""}${esc(o.label || `Option ${i + 1}`)} <span class="small">· ${t}</span></span>`
          : `<label class="seg${i === src.option ? " on" : ""}"><input type="radio" name="rt-opt" value="${i}"${i === src.option ? " checked" : ""}${ro ? " disabled" : ""}> ${esc(o.label || `Option ${i + 1}`)}</label>`;
      }).join("")}</div>` : ""}
      ${src.step ? `<p class="m0 rt-step" role="status">${esc(src.step)}</p>` : ""}
      ${ro ? "" : `<p class="m0"><button type="button" class="linkbtn small" data-rt-discard>${(src.taken || []).includes("saved") ? "Set aside the rest of this reading" : "Set aside this reading"}</button> <span class="muted small">when it is not their rates (a question back, the wrong mail)</span></p>`}</div>`;
  }
  // the totals and what each line comes to follow every figure, without redrawing what is being typed
  function refresh() {
    if (!dr || !card) return;
    const T = R.totalOf(card.lines, autoFn);
    if (q("rt-total")) q("rt-total").textContent = T.any ? `${card.cur} ${Q.money(T.t)}` : "-";
    if (q("rt-atcost")) q("rt-atcost").textContent = T.atCost ? "plus the AT COST lines" : "";
    dr.querySelectorAll("#rt-ls .rl").forEach(el => {
      const l = card.lines[Number(el.dataset.i)]; if (!l) return;
      const auto = autoFn(l.unit), a = R.amountOf(l, auto);
      const qi = el.querySelector('[data-f="qty"]'), ai = el.querySelector('[data-f="amount"]');
      if (qi) qi.placeholder = auto === null ? "1" : String(auto);
      if (ai) ai.placeholder = a && !a.hand ? (a.n !== undefined ? Q.money(a.n) : a.w) : "";
      let more = el.querySelector(".more");
      const note = a?.min ? "The minimum applies" : a?.hand ? "Amount set by hand" : "";
      if (note && !more) { more = document.createElement("div"); more.className = "more"; el.append(more); }
      if (more) { if (note) more.innerHTML = `<span class="hand">${note}</span>`; else more.remove(); }
      el.classList.toggle("miss", !!(tried && R.used([l]).length && !a));
    });
  }


  // ------------------------------------------------------------------ events
  function wire() {
    dr.addEventListener("click", async ev => {
      if (ev.target.closest("[data-rt-x]")) { close(); return; }
      if (ev.target.closest("#rt-add")) {
        if (card.lines.length >= R.MAX_LINES) return;
        const seg = card.lines[card.lines.length - 1]?.seg || "Freight";
        card.lines.push(R.blankLine(seg)); dirty = true; paint();
        dr.querySelector(`.rl[data-i="${card.lines.length - 1}"] [data-f="charge"]`)?.focus(); return;
      }
      const rm = ev.target.closest("[data-rm]");
      if (rm) { card.lines.splice(Number(rm.dataset.rm), 1); if (!card.lines.length) card.lines.push(R.blankLine()); dirty = true; paint(); return; }
      if (ev.target.closest("#rt-save")) { save(); return; }
      if (ev.target.closest("#rt-skip")) { skip(); return; }
      // typing by hand, another of the vendor's options, or a new one
      const ok = ev.target.closest("[data-rt-optkey], [data-rt-optadd]");
      if (ok && !src) {
        const add = ok.hasAttribute("data-rt-optadd"), label = add ? "" : ok.dataset.rtOptkey;
        if (!add && card.fixed && R.labelKey(label) === R.labelKey(card.label)) return;
        const go = () => { card = cardFor(cur(), card.vendor, card.route, label, add); dirty = false; tried = false; paint(); if (add) q("rt-label")?.focus(); };
        if (dirty && !ro) confirmBox("Another option", "Open the other option? What you changed in this one is not kept.", "Open it").then(y => { if (y) go(); });
        else go();
        return;
      }
      if (ev.target.closest("[data-rt-discard]")) { discard(); }
    });
    scrim.addEventListener("click", () => close());
    dr.addEventListener("input", ev => {
      const el = ev.target;
      if (ro || !card) return;
      const row = el.closest(".rl");
      if (row && el.dataset.f) { card.lines[Number(row.dataset.i)][el.dataset.f] = el.value; dirty = true; refresh(); return; }
      const f = { "rt-transit": "transit", "rt-carrier": "carrier", "rt-free": "free", "rt-note": "note", "rt-label": "label" }[el.id];
      if (f) { card[f] = el.value; dirty = true; }
    });
    dr.addEventListener("change", ev => {
      const el = ev.target;
      if (ro || !card) return;
      const row = el.closest(".rl");
      if (row && el.dataset.f === "seg") { card.lines[Number(row.dataset.i)].seg = el.value; dirty = true; return; }
      // a unit picked from the list: the quantity and the amount follow (refresh), nothing is redrawn under the cursor
      if (row && el.dataset.f === "unit") { card.lines[Number(row.dataset.i)].unit = el.value; dirty = true; refresh(); return; }
      if (el.dataset.said) { card.said[el.dataset.said] = el.value; dirty = true; el.closest(".said-row")?.querySelectorAll(".seg").forEach(s => s.classList.toggle("on", s.querySelector("input").checked)); return; }
      if (el.id === "rt-cur") { card.cur = el.value; dirty = true; refresh(); return; }
      if (el.id === "rt-on") { card.on = el.value; dirty = true; return; }
      if (el.id === "rt-valid") { card.valid = el.value; dirty = true; return; }
      if (el.name === "rt-opt" && src) {
        const i = Number(el.value);
        const go = () => { card = cardFromDraft(cur(), card.vendor, src.draft, i); src.option = i; src.step = ""; dirty = false; tried = false; paint(); };
        if (dirty) confirmBox("Another option", "Switch to the other option? What you changed in this one is not kept.", "Switch").then(ok => { if (ok) go(); else paint(); });
        else go();
        return;
      }
      // a draft keeps its lines when its route is corrected; OppList records that the route was changed
      if (el.id === "rt-route" && src) { card.route = Number(el.value); dirty = true; paint(); return; }
      if (el.id === "rt-route") {
        const n = Number(el.value);
        const go = () => { card = cardFor(cur(), card.vendor, n); dirty = false; tried = false; paint(); };
        if (dirty) confirmBox("Another route", "Switch to the other route? What you typed for this one is not kept.", "Switch").then(ok => { if (ok) go(); else el.value = String(card.route); });
        else go();
      }
    });
  }

  // ------------------------------------------------------------------ saving
  async function save() {
    if (saving || ro) return;
    const today = todayIso(), err = q("rt-err");
    const probs = R.problems(card, { today, existing: existing() });
    if (probs.length) { tried = true; paint(); dr.querySelector(".dr-body").scrollTop = 0; return; }
    const c = cur();
    if (!canRates(c)) { if (err) err.textContent = "This case is no longer open, so the rates were not saved."; return; }
    saving = true; const btn = q("rt-save"); if (btn) btn.disabled = true;
    try {
      const doc = R.docOf(card, { opp, autoFn });
      // saved from a Read quote draft: its id, the option in the drawer and how
      if (src) Object.assign(doc, { draft: src.draft.id, option: src.option, how: "read" });
      await act("rates", opp, { vendor: doc.vendor, cur: doc.cur, total: doc.total, valid: doc.valid, ...("label" in doc ? { label: doc.label } : {}) }, { rates: doc });
      const msg = `Rates from ${card.vendor}${doc.label ? `, ${doc.label},` : ""} saved${typeof doc.total === "number" ? ` (${doc.cur} ${Q.money(doc.total)})` : ""}.`;
      // a reading of several options goes on to the next one not taken
      if (src && optsOn()) {
        src.taken[src.option] = "saved";
        const next = R.nextUntaken(src.taken, src.option);
        if (next >= 0) {
          const was = doc.label || `Option ${src.option + 1}`;
          card = cardFromDraft(cur(), card.vendor, src.draft, next); src.option = next;
          src.step = `Option ${next + 1} of ${src.taken.length}. “${was}” is saved. Check this one and save it too, or skip it if it will not be used.`;
          saving = false; dirty = false; tried = false; paint(); dr.querySelector(".dr-body").scrollTop = 0;
          onDone?.("progress", msg); return;
        }
      }
      hide(); onDone?.("saved", msg);
    } catch (e) {
      saving = false; if (btn) btn.disabled = false;
      if (q("rt-err")) q("rt-err").innerHTML = failHtml(e, { part: opp });
    }
  }

  // a reading that is not their rates leaves the row (OppList records it set aside). Once an option is
  // saved, the rest of it
  async function discard() {
    if (!src || ro || saving) return;
    const some = (src.taken || []).includes("saved");
    if (!await confirmBox(some ? "Set aside the rest" : "Set aside this reading", some
      ? "Set aside the rest of this reading? The options saved stay as their rates; the others are not kept."
      : "Set this reading aside? It leaves the row and nothing of it is kept as their rates. The mail stays where it is.", "Set aside")) return;
    saving = true;
    const who = src.draft.vendor || card.vendor;
    try { await act("discard", opp, { draft: src.draft.id }); hide(); onDone?.(some ? "saved" : "discarded", some ? `The rest of the reading of ${who}'s mail is set aside.` : `The reading of ${who}'s mail is set aside.`); }
    catch (e) { saving = false; if (q("rt-err")) q("rt-err").innerHTML = failHtml(e, { part: opp }); }
  }
  // an option of the reading that will not be used: a discard with the option, then the next one
  async function skip() {
    if (!src || ro || saving || !optsOn()) return;
    if (dirty && !await confirmBox("Skip this option", "Skip this option? What you changed in it is not kept.", "Skip it")) return;
    saving = true;
    const who = src.draft.vendor || card.vendor, was = txt(card.label) || `Option ${src.option + 1}`;
    try {
      await act("discard", opp, { draft: src.draft.id, option: src.option });
      src.taken[src.option] = "skipped";
      const next = R.nextUntaken(src.taken, src.option);
      if (next >= 0) {
        card = cardFromDraft(cur(), card.vendor, src.draft, next); src.option = next;
        src.step = `Option ${next + 1} of ${src.taken.length}. “${was}” is skipped. Check this one and save it, or skip it too.`;
        saving = false; dirty = false; tried = false; paint(); return;
      }
      const some = src.taken.includes("saved");
      hide(); onDone?.(some ? "saved" : "discarded", some ? `The rest of the reading of ${who}'s mail is set aside.` : `The reading of ${who}'s mail is set aside.`);
    } catch (e) { saving = false; if (q("rt-err")) q("rt-err").innerHTML = failHtml(e, { part: opp }); }
  }

  return {
    show, leave: hide, isOpen: () => open,
    current: () => open && card ? { vendor: card.vendor, route: card.route, draft: src?.draft?.id || "" } : null,
    // the case changed while the drawer is open: only what does not disturb the typing (a case that closed turns it
    // read only at the next opening; saving then says why not)
    refresh() { if (open && !cur()) hide(); },
  };
}
// a saved version's day for "saved on": the local day of its time
const stampDay = v => { const t = v?.toMillis ? v.toMillis() : v instanceof Date ? v.getTime() : 0; if (!t) return ""; const d = new Date(t); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
