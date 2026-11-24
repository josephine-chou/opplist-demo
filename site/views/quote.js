// Quote to requester: a drawer from the right, like Ask vendors, in two
// steps, opened from the case's Update this case once OppList pushes its quote rules (meta/sync.quoteRules).
//   1 Quote: the lines as on our quotation sheet (Charge, Qty, Unit, Rate, Amount, Remarks; Qty x Rate, a typed amount,
//     AT COST and the other words), one currency, Valid until, transit, routing, remarks and the company's terms. For
//     the admin and people ticked for Costs, the cost block on top: each line's Cost and Markup (OppList's rule, none
//     for one of our offices), Rate = Cost + Markup, Cost, Sell, Profit, Margin and the warnings (never a stop).
//   2 Mail: To, CC, the subject and the mail as it will read (Hi {first name}, the table, the validity, the terms), the
//     PDF attached. Create draft in Outlook copies the request for Make Outlook drafts (v 2: a Reply All under the
//     requester's mail, found by its Message-ID), downloads the PDF to Downloads, and logs the quote sent today: one
//     quote action with quoteId (OppList takes it in as Quoted or Re-quoted) written together with
//     quotes/{id} (the quote, for everyone) and quoteCosts/{id} (its costs, admin and Costs only).
// Just log it: the Log quote sent form. No helper on this computer: copy the quote
// and the subject, download the PDF, send it from Outlook, then Log quote sent (the quote is saved the same way).
// The card being written stays in this browser until the quote is saved; a case's next quote starts from its last one.
// Never in the mail, the PDF or the clipboard: a cost, a markup, the profit or the margin.
import { S, myOwner, canCosts, confirmBox, modal } from "../core.js";
import { esc, day, todayIso, validIso, plural, nowMs } from "../lib.js";
import { act, newActionId } from "../data.js";
import { failHtml } from "../issues.js";
import { ownerOptions, helperBox } from "./common.js";
import * as Q from "../quotemail.js";
import { cleanList, nameOf } from "../askmail.js";
import { quotePdf, downloadBlob, openBlob, loadPdf } from "../quotepdf.js";
import * as R from "../ratelib.js";
import { letter } from "../editlib.js";

const PREF_TEAM = "opplist.ask.teamCc";      // the CC #JJC-MFN choice, shared with Ask vendors
// the card being written, in this browser only and for this person (a shared browser does not show it to another)
const DRAFT = opp => `opplist.quote.${S.me?.email || ""}.${opp}`;
const keep = {
  get(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window: the card is not kept */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* nothing kept */ } },
};
const prefTeam = () => { try { return localStorage.getItem(PREF_TEAM) !== "0"; } catch { return true; } };
const setPrefTeam = on => { try { localStorage.setItem(PREF_TEAM, on ? "1" : "0"); } catch { /* private window */ } };
const STEPS = ["Quote", "Mail"];
const ms = v => v?.toMillis ? v.toMillis() : v instanceof Date ? v.getTime() : Number(v) || 0;

// Rates from sits at the top of step 1, with the cost block under it; a
// vendor picked for a part whose rates are typed on the case fills that part's lines (Use their rates: its rates as the
// costs, the markup by the rule), for people with Costs; everyone sees the vendor's total there.
// Picking that vendor, or opening a card with it picked, lays them in at once when no line they would replace
// has a price typed; else Use their rates
export function quotePanel(opp, cur, { quotes = () => [], costsOf = () => null, rates = () => ({ best: new Map() }), drafts = () => [], onCheck = null, onDone } = {}) {
  let open = false, card = null, step = 1, view = "card", tried = false, done = null, reqId = "", handPdf = "", dr = null, scrim = null, keepT = 0, dirty = false;
  const q = id => dr?.querySelector(`#${id}`) || null;
  const rules = () => S.meta?.quoteRules || {};
  // a vendor's options, once OppList keeps them; a part's label is
  // the option it uses (null: not picked, the lowest)
  const optsOn = () => S.meta?.options === true;
  const pick = s => R.ratesForQuote(rates().best, s.key, cur() || {}, optsOn() ? s.label ?? null : null);
  const teamAddr = () => cleanList([S.meta?.teamCc || ""])[0] || "";
  const nextN = () => quotes().length + 1;
  const sentBy = () => card?.sentBy || myOwner();
  // their mail as the quote may use it: never a vendor of the case, no Reply All when one is on it
  const rep = () => { const c = cur() || {}; return { ...(c.reply || {}), ...Q.guardReply(c, S.vendors?.rows || [], teamAddr()) }; };
  // the case's vendors, for Rates from
  const vendorKeys = () => [...new Set((cur()?.rr || []).filter(x => x.vendor && !x.pending).map(x => x.vendor))];
  // a saved quote's parts on the card; a vendor no longer on the case becomes a name typed
  function srcCard(list) {
    const keys = vendorKeys();
    return Q.sourcesForCard(list).map(s => s.key && s.key !== "own" && s.key !== "other" && !keys.includes(s.key) ? { ...s, key: "other", name: s.name || s.key } : s);
  }

  // ------------------------------------------------------------------ the card
  // a new card: from the case's last quote (its costs too for people who can read them), else from the case
  function fresh(c, fromCase = false) {
    const last = fromCase ? null : quotes().slice(-1)[0] || null, costs = canCosts();
    const lines = last ? Q.linesFromQuote(last, costs ? costsOf(last.id) : null) : Q.startLines(c);
    if (!lines.length) lines.push(Q.line("", "1", ""));
    const g = Q.guardReply(c, S.vendors?.rows || [], teamAddr());
    return { v: 1, cur: last?.cur || "USD", lines, valid: "", transit: last?.transit || "", routing: last?.routing || "", other: last?.remarks || "",
      terms: last ? termsFrom(last.terms) : Q.startTerms(c, lines), opening: Q.openingOf(c), closing: Q.CLOSING,
      to: g.to.join("; "), cc: g.cc.join("; "), teamCc: prefTeam(), subject: Q.subjectOf(c, opp), sources: srcCard(last?.sources),
      attach: true, logIt: true, pct: Q.startPct(c, rules()), costs, from: last ? `Quote ${last.n}` : "", sentBy: myOwner(), termsOpen: false };
  }
  // the terms of a saved quote, by their words
  function termsFrom(texts) {
    const have = new Set(texts || []);
    return Object.fromEntries(Q.TERMS.map(x => [x.k, x.k === "cur" ? (texts || []).some(t => /^Rates are in /.test(t)) : have.has(x.t({ cur: "USD" }))]));
  }
  function start(c) {
    const kept = keep.get(DRAFT(opp)), last = quotes().slice(-1)[0];
    // the card left in this browser, unless a quote was saved on the case after it
    if (kept?.v === 1 && Array.isArray(kept.lines) && (!last || ms(last.at) < (kept.keptAt || 0))) {
      return { ...fresh(c), ...kept, costs: canCosts(), from: kept.from || "", sources: Array.isArray(kept.sources) && kept.sources.length ? kept.sources : [Q.source("Freight")] };
    }
    return fresh(c);
  }
  // what was typed is kept (in this browser) a moment after each change, and when the drawer closes
  function changed() { dirty = true; clearTimeout(keepT); keepT = setTimeout(() => { if (card && view === "card") keep.set(DRAFT(opp), { ...card, keptAt: nowMs() }); }, 500); }

  // ------------------------------------------------------------------ opening and closing
  // use, a vendor picked in Compare rates (Use in the quote)
  function show(how = "card", { use = "", label = null } = {}) {
    if (open) {
      if (how === "just" && view !== "done") { view = "just"; paint(); }
      else if (use && view === "card" && useVendor(use, label)) keepScroll(paint);
      return;
    }
    const c = cur(); if (!c) return;
    open = true;
    scrim = document.createElement("div"); scrim.className = "ask-scrim";
    dr = document.createElement("aside"); dr.className = "ask-dr qc-dr";
    dr.setAttribute("role", "dialog"); dr.setAttribute("aria-modal", "false"); dr.setAttribute("aria-labelledby", "qc-title");
    dr.innerHTML = `<div class="dr-head"><div class="t"><h2 id="qc-title" tabindex="-1"></h2><div class="dr-sub"></div></div>
        <button type="button" class="xbtn" data-qc-x aria-label="Close">&times;</button></div>
      <div class="dr-steps" role="tablist" aria-label="Steps"></div>
      <div class="dr-body"></div>
      <div class="dr-foot"></div>
      <datalist id="qc-dl-charge">${Q.CHARGES.map(x => `<option value="${esc(x)}">`).join("")}</datalist>
      <datalist id="qc-dl-unit">${Q.UNITS.map(x => `<option value="${esc(x)}">`).join("")}</datalist>`;
    document.body.append(scrim, dr);
    card = start(c); step = 1; view = how === "just" ? "just" : "card"; tried = false; done = null; reqId = ""; handPdf = ""; dirty = false;
    // Use in the quote from Compare rates
    if (view === "card" && use) useVendor(use, label);
    // a vendor already picked whose rates were saved since goes in now (kept in this browser on closing)
    if (view === "card" && card.sources.map((_, k) => follow(k)).some(Boolean)) dirty = true;
    wire(); paint();
    requestAnimationFrame(() => requestAnimationFrame(() => dr?.classList.add("open")));
    document.addEventListener("keydown", onKey, true);
    setTimeout(() => q("qc-title")?.focus(), 260);
    loadPdf().catch(() => { /* tried again when the PDF is made */ });
  }
  function hide() {
    if (!open) return;
    if (card && view === "card" && dirty) keep.set(DRAFT(opp), { ...card, keptAt: nowMs() });
    clearTimeout(keepT); open = false;
    document.removeEventListener("keydown", onKey, true);
    dr?.remove(); scrim?.remove(); dr = scrim = null; card = null; done = null;
  }
  function close() { hide(); onDone?.("closed"); document.querySelector("[data-quote-open]")?.focus(); }
  function onKey(ev) { if (ev.key === "Escape" && open && !document.querySelector(".modal")) { ev.preventDefault(); close(); } }

  // ------------------------------------------------------------------ drawing
  function paint() {
    if (!dr || !card) return;
    const c = cur() || {};
    q("qc-title").textContent = view === "just" ? "Log quote sent" : `Quote to ${c.req || "the requester"}`;
    dr.querySelector(".dr-sub").textContent = [opp, Q.modeOf(c), endsText(c), Q.cargoPhrase(c), Q.incotermOf(c)].filter(Boolean).join(" · ");
    const steps = dr.querySelector(".dr-steps");
    steps.hidden = view !== "card"; steps.innerHTML = view === "card" ? tabs() : "";
    const body = dr.querySelector(".dr-body");
    body.innerHTML = view === "just" ? justHtml() : view === "done" ? doneHtml() : step === 1 ? quoteHtml() : mailHtml();
    body.scrollTop = 0;
    paintFoot();
    if (view === "card" && step === 2) { fit(q("qc-open")); fit(q("qc-close")); }
  }
  const endsText = c => { const e = (c.routes || [])[0]; return e && (e.from || e.to) ? `${Q.placeName(e.from)} to ${Q.placeName(e.to)}` : c.lane || ""; };
  function tabs() {
    const n = Q.used(card).length;
    return STEPS.map((label, i) => {
      const k = i + 1, on = step === k, ok = k < step;
      return `<button type="button" role="tab" class="stab${on ? " on" : ""}${ok ? " ok" : ""}" data-step="${k}" aria-selected="${on}"><span class="n" aria-hidden="true">${ok ? "&#10003;" : k}</span>`
        + `<span class="lbl">${label}${k === 1 ? ` <span class="c" id="qc-count">· ${plural(n, "line")}</span>` : ""}</span></button>`;
    }).join("");
  }
  function paintFoot() {
    const foot = dr.querySelector(".dr-foot");
    if (view === "done") { foot.innerHTML = `${done?.how === "draft" ? '<button type="button" class="btn" id="qc-again">Copy for Outlook again</button>' : ""}<span class="grow"></span><button type="button" class="btn primary" id="qc-done">Done</button><span class="err" id="qc-err" role="alert"></span>`; return; }
    if (view === "just") { foot.innerHTML = `<span class="err" id="qc-err" role="alert"></span><button type="button" class="btn" id="qc-back-card">&lsaquo; Back to the card</button><span class="grow"></span><button type="button" class="btn primary" id="qc-jl-save">Save</button>`; return; }
    if (step === 1) {
      foot.innerHTML = `<span class="err" id="qc-err" role="alert"></span><span class="info">${card.costs ? "Saved on the case when you create the draft; the costs only for people with Costs access." : "Saved on the case when you create the draft."}</span>`
        // Just log it is a button beside Next: Mail; the button says only Just log it
        + `<span class="qc-pair"><button type="button" class="btn" id="qc-just" title="Sent the quote another way (our Excel quotation, a phone call)? Log it as sent, without a draft.">Just log it</button>`
        + `<button type="button" class="btn primary" data-go="2">Next: Mail &rsaquo;</button></span>`;
      return;
    }
    const probs = probsNow();
    foot.innerHTML = `<span class="err" id="qc-err" role="alert"></span><button type="button" class="btn" data-go="1">&lsaquo; Back</button><span class="grow"></span>`
      + (myOwner() ? "" : `<label class="fld-in small">Sent by <select id="qc-by" aria-label="Sent by">${ownerOptions(card.sentBy || "", "Pick who sends it")}</select></label>`)
      + `<label class="chk small"><input type="checkbox" id="qc-log"${card.logIt ? " checked" : ""}> Log as quote sent today</label>`
      + `<button type="button" class="btn primary" id="qc-make"${probs.length ? " disabled" : ""}>Create draft in Outlook</button>`;
  }
  const footErr = msg => { const e = q("qc-err"); if (e) e.innerHTML = msg; };
  function fit(t) { if (!t) return; t.style.height = "auto"; t.style.height = `${Math.max(48, t.scrollHeight + 2)}px`; }
  const probsNow = () => Q.problems(card, { needTo: !rep().mid, today: todayIso() });

  // ---- step 1: the quote
  function quoteHtml() {
    const c = cur() || {}, mode = Q.modeOf(c), T = Q.totals(card), cw = mode === "Air" ? Q.chargeable(c) : null, w = mode === "LCL" ? Q.wm(c) : null;
    const have = new Set(card.lines.map(l => String(l.charge || "").trim().toUpperCase()));
    const chips = (Q.SUGGEST[mode] || []).filter(x => !have.has(x[0]));
    const nTerms = Q.TERMS.filter(x => card.terms[x.k]).length, today = todayIso();
    return `${sourcesHtml()}${card.costs ? costHtml() : ""}
      <div class="qtop"><label class="fld-in">Currency <select id="qc-cur">${Q.CURRENCIES.map(x => `<option${x === card.cur ? " selected" : ""}>${x}</option>`).join("")}</select></label>
        <span class="muted small">${card.from ? `Lines from ${esc(card.from)} on this case. Change anything.` : `Lines as on our quotation sheet: the freight from the case's cargo, what ${esc(Q.incotermOf(c) || "the terms")} adds, and our handling fee. Change anything.`}</span></div>
      <div class="qc-lines"><div class="qh${card.costs ? " full" : ""}" aria-hidden="true"><span>Charge</span><span class="r">Qty</span><span>Unit</span>${card.costs ? '<span class="r">Cost</span><span class="r">Markup</span>' : ""}<span class="r">Rate</span><span class="r">Amount</span><span></span></div>
        <div id="qc-ls">${card.lines.map(lineHtml).join("")}</div></div>
      <div class="qadd"><button type="button" class="btn small" id="qc-add">+ Add a line</button>${chips.length ? `<span class="lbl">Often added:</span>${chips.map(x => `<button type="button" class="qpill" data-chip="${esc(x.join("|"))}">+ ${esc(x[0])}</button>`).join("")}` : ""}</div>
      <div class="qtot"><span class="muted">Total to the requester</span><span class="v" id="qc-total">${esc(card.cur)} ${Q.money(T.t)}</span><span class="muted small" id="qc-atcost">${T.atCost ? "plus the AT COST lines" : ""}</span></div>
      ${cw ? `<p class="cw">Chargeable weight <b>${cw.cw} kg</b>: ${cw.okCbm ? `${cw.cbm.toFixed(2)} cbm${cw.fromDims ? " (from the dimensions)" : ""} x 167 = ${Math.ceil(cw.vol)} kg` : "no volume on the case"}, ${cw.okKg ? `${Math.round(cw.kg)} kg actual` : "no weight on the case"}; the larger counts. Check it against the dimensions before you send.</p>` : ""}
      ${w ? `<p class="cw">W/M <b>${w.wm.toFixed(2)}</b>: ${w.cbm.toFixed(2)} cbm or ${(w.kg / 1000).toFixed(2)} t, the larger counts (1 cbm = 1,000 kg). Vendors do not all count it the same way.</p>` : ""}
      <section class="qc-sec" aria-labelledby="qc-h-valid"><h3 id="qc-h-valid">Validity and transit</h3>
        <div class="row2">
          <div class="field"><span>Valid until <span class="req" aria-hidden="true">*</span></span><input type="date" id="qc-valid" value="${esc(card.valid)}" min="${today}"${tried && !card.valid ? ' aria-invalid="true"' : ""}>
            <div class="quick"><button type="button" class="qpill" data-days="7">In 7 days</button><button type="button" class="qpill" data-days="14">In 14 days</button><button type="button" class="qpill" data-days="end">End of month</button></div></div>
          <label class="field">Transit time<input type="text" id="qc-transit" value="${esc(card.transit)}" maxlength="100" placeholder="e.g. 30 to 35 days"></label>
        </div>
        <div class="row2">
          <label class="field">Routing (optional)<input type="text" id="qc-routing" value="${esc(card.routing)}" maxlength="200" placeholder="e.g. via Panama"></label>
          <label class="field">Remarks (optional)<input type="text" id="qc-other" value="${esc(card.other)}" maxlength="1000" placeholder="e.g. 14 free days at destination"></label>
        </div></section>
      <details class="terms" id="qc-terms"${card.termsOpen ? " open" : ""}><summary>Terms <span class="c">· ${nTerms} of ${Q.TERMS.length} ticked, the company's standard lines</span></summary>
        <div class="tlist">${Q.TERMS.map(x => `<label class="chk"><input type="checkbox" data-term="${x.k}"${card.terms[x.k] ? " checked" : ""}><span>${esc(x.t(card))}</span></label>`).join("")}</div></details>
      <p class="small m0"><button type="button" class="linkbtn small" id="qc-reset">Start again from the case</button></p>`;
  }
  function lineHtml(l, i) {
    const a = Q.amountOf(l, card.costs), auto = a && !a.hand ? (a.n !== undefined ? Q.money(a.n) : a.w) : "";
    const hasCost = card.costs && Q.num(l.cost) !== null, r = Q.rateOf(l, card.costs), miss = tried && String(l.charge || "").trim() && !a;
    const rateCell = hasCost ? `<div class="rate r" data-l="Rate"><span class="ro" title="Cost + markup">${r?.n !== undefined ? Q.rateText(r.n) : ""}</span></div>`
      : `<div class="rate r" data-l="Rate"><input type="text" inputmode="decimal" aria-label="Rate" data-f="rate" value="${esc(l.rate)}" maxlength="20"></div>`;
    const costCells = card.costs ? `<div class="cst r" data-l="Cost"><input type="text" inputmode="decimal" aria-label="Cost" data-f="cost" value="${esc(l.cost)}" maxlength="20" placeholder="cost"></div>
      <div class="mkp r" data-l="Markup"><input type="text" inputmode="decimal" aria-label="Markup" data-f="markup" value="${esc(l.markup)}" maxlength="20"${hasCost ? "" : " disabled"}></div>` : "";
    return `<div class="ql${card.costs ? " full" : ""}${miss ? " miss" : ""}" data-i="${i}">
      <div class="ch"><input type="text" list="qc-dl-charge" aria-label="Charge" data-f="charge" value="${esc(l.charge)}" maxlength="80" placeholder="e.g. OCEAN FREIGHT"></div>
      <div class="q r" data-l="Qty"><input type="text" inputmode="decimal" aria-label="Qty" data-f="qty" value="${esc(l.qty)}" maxlength="20"></div>
      <div class="unit-cell" data-l="Unit"><div class="unit"><span>per</span><input type="text" list="qc-dl-unit" aria-label="Unit" data-f="unit" value="${esc(l.unit)}" maxlength="20"></div></div>
      ${costCells}${rateCell}
      <div class="amt r" data-l="Amount"><input type="text" inputmode="decimal" aria-label="Amount" data-f="amount" value="${esc(l.amount)}" maxlength="20" placeholder="${esc(auto)}" title="Qty x Rate. Type an amount to set it yourself (a minimum, say)."></div>
      <div class="xcell"><button type="button" class="x" data-rm="${i}" aria-label="Remove this line" title="Remove this line">&times;</button></div>
      <div class="more">${l.open || l.remark ? `<input type="text" aria-label="Remark" data-f="remark" value="${esc(l.remark)}" maxlength="200" placeholder="Remark, e.g. Includes fuel surcharge">` : `<button type="button" class="linkbtn small" data-open="${i}">Remark</button>`}<span class="notes">${notesOf(l)}</span></div>
    </div>`;
  }
  // under a line: an amount typed by hand; an own fee (no cost) once costs are being typed on the card
  function notesOf(l) {
    const a = Q.amountOf(l, card.costs), r = Q.rateOf(l, card.costs);
    const costing = card.costs && card.lines.some(x => Q.num(x.cost) !== null);
    return `${a?.hand ? '<span class="hand">Amount set by hand</span>' : ""}${costing && Q.num(l.cost) === null && String(l.charge || "").trim() && r?.n !== undefined ? '<span class="own">No cost: all of it is margin</span>' : ""}`;
  }
  // ---- whose rates: one line a part, for the case; never in the mail, the PDF or Make Outlook drafts
  function sourcesHtml() {
    return `<section class="qc-sec qc-src" aria-labelledby="qc-h-src"><h3 id="qc-h-src">Rates from <span>for the case, never in the mail or the PDF</span></h3>
      <div id="qc-srcs">${card.sources.map(srcHtml).join("")}</div>
      ${card.sources.length < Q.MAX_SOURCES ? '<button type="button" class="btn small" id="qc-src-add">+ Add a part</button>' : ""}</section>`;
  }
  function srcHtml(s, i) {
    const keys = vendorKeys(), k = s.key || "", other = k === "other";
    return `<div class="src" data-s="${i}">
      <select data-sf="seg" aria-label="Part">${Q.SEGMENTS.map(x => `<option${x === s.seg ? " selected" : ""}>${x}</option>`).join("")}</select>
      <select data-sf="key" aria-label="Whose rate"><option value="">Whose rate?</option>${keys.map(v => `<option value="${esc(v)}"${v === k ? " selected" : ""}>${esc(v)}</option>`).join("")}<option value="own"${k === "own" ? " selected" : ""}>Our own rate</option><option value="other"${other ? " selected" : ""}>Someone else</option></select>
      ${other ? `<input type="text" data-sf="name" aria-label="Who" value="${esc(s.name)}" maxlength="80" placeholder="e.g. a co-loader, a trucker">` : ""}
      <input type="text" data-sf="carrier" aria-label="Carrier" value="${esc(s.carrier)}" maxlength="40" placeholder="Carrier, e.g. CK">
      <button type="button" class="x" data-src-rm="${i}" aria-label="Remove this part" title="Remove this part">&times;</button>${optionHtml(s)}${theirRates(s, i)}</div>`;
  }
  // which of the vendor's options, when it has two or more on the route the card uses
  function optionHtml(s) {
    if (!optsOn() || !s.key || s.key === "own" || s.key === "other") return "";
    const list = R.quoteOptions(rates().best, s.key, cur() || {});
    if (list.length < 2) return "";
    const d = pick(s), on = d ? R.labelKey(d.label) : "";
    return `<div class="src-opt"><span class="muted small">Their option</span><select data-sf="label" aria-label="Their option">${list.map(o =>
      `<option value="${esc(o.label || "")}"${R.labelKey(o.label) === on ? " selected" : ""}>${esc(o.label || "Unnamed")}${typeof o.total === "number" ? ` (${esc(o.cur)} ${Q.money(o.total)})` : ""}</option>`).join("")}</select></div>`;
  }
  // the vendor's rates saved on the case, under its part: the total, valid until; Use their rates for Costs.
  // Once they are in the costs, it says so and offers to lay them in again
  function theirRates(s, i) {
    if (!s.key || s.key === "own" || s.key === "other") return "";
    const c = cur() || {}, d = pick(s);
    if (!d) {
      // a reading of their mail not checked yet is offered here
      const w = (drafts() || []).filter(x => x.vendor === s.key), n = w.reduce((k, x) => k + (x.options || []).length, 0);
      return w.length && onCheck
        ? `<div class="src-rt small"><span class="muted">No rates from ${esc(s.key)} saved on the case yet.</span> <span class="rq-wait">A reading of their mail waits to be checked (${plural(n, "option")}):</span> <button type="button" class="btn small" data-src-check="${esc(w[0].id)}">Check it</button></div>`
        : `<div class="src-rt muted small">No rates from ${esc(s.key)} saved on the case yet (Add rates on its Rate requests row).</div>`;
    }
    const n = d.lines?.filter(l => l.seg === s.seg).length || 0, multi = R.routesOf(c).length > 1;
    const what = `${multi && d.route ? `Route ${esc(letter(d.route))}: ` : ""}${esc(R.totalText(d))}${d.valid ? `, valid to ${esc(day(d.valid))}` : ""}`;
    // in the costs, all of them or all but the lines changed by hand (those stay until the button)
    const use = !n ? ` <span class="muted">· no ${esc(s.seg)} lines in them</span>`
      : s.filled === filledKey(d, s) && s.held ? ` <span class="muted">· in the costs below, but not over ${plural(s.held, "line")} you changed</span> <button type="button" class="btn small" data-src-fill="${i}">Use theirs there too</button>`
      : s.filled === filledKey(d, s) ? ` <span class="muted">· in the costs below</span> <button type="button" class="linkbtn small" data-src-fill="${i}">Use them again</button>`
      : ` <button type="button" class="btn small" data-src-fill="${i}">Use their ${esc(s.seg)} rates (${n} line${n === 1 ? "" : "s"})</button>`;
    return `<div class="src-rt small"><span class="muted">Their rates on the case: ${what}${R.notSaid(d).length ? ` · ${esc(R.notSaid(d).join(", "))}` : ""}</span>`
      + (card.costs && view === "card" ? use : "") + `</div>`;
  }
  // which version of their rates went in for which part (kept on the card in this browser, never saved)
  const filledKey = (d, s) => `${d.id}|${s.seg}`;
  // their lines of that part go into the card: the same charge and unit replaced, the rest added before our handling
  // fee; each marked with the part and version (via) so they can follow the vendor picked; `only` the lines that
  // go in (the rest held: lines changed by hand they would replace)
  function lay(s, d, only = null) {
    const all = R.quoteLines(d, s.seg, { pct: card.pct }), add = only || all;
    if (!add.length) return false;
    card.lines = R.mergeQuoteLines(card.lines, add, filledKey(d, s)).slice(0, Q.MAX_LINES);
    if (!String(s.carrier || "").trim() && d.carrier) s.carrier = String(d.carrier).slice(0, 40);
    s.filled = filledKey(d, s); s.held = all.length - add.length;
    return true;
  }
  // A part's costs follow its vendor. What it laid in and nobody changed goes when another
  // vendor (or none, or a newer version of their rates, or another part) is picked: back to what the line was, or out
  // when the vendor added it; a line changed by hand stays. Then the vendor picked goes in at once (autoFill)
  function unlay(s) {
    if (s?.filled) { card.lines = R.unlayLines(card.lines, s.filled); s.filled = ""; s.held = 0; }
  }
  function follow(i) {
    const s = card?.sources?.[i]; if (!s || !card.costs || view !== "card") return false;
    const d = !s.key || s.key === "own" || s.key === "other" ? null : pick(s);
    if (s.filled && (!d || s.filled !== filledKey(d, s))) unlay(s);
    return autoFill(i);
  }
  // a vendor picked in Rates from, or already picked when the card opens, whose rates are
  // saved on the case goes in at once: once a version and part, for people with Costs, in the quote's currency, and
  // only when no line it would replace has a cost, a rate or an amount typed (else Use their rates)
  function autoFill(i) {
    const s = card?.sources?.[i];
    if (!s || !card.costs || view !== "card" || !s.key || s.key === "own" || s.key === "other") return false;
    const d = pick(s);
    if (!d || s.filled === filledKey(d, s) || d.cur !== card.cur) return false;
    const add = R.quoteLines(d, s.seg, { pct: card.pct }), held = R.typedFor(card.lines, add);
    // the lines that replace nothing typed go in now; those that would replace a line changed by hand wait
    const free = add.filter(a => !held.includes(a));
    if (!free.length) return false;
    return lay(s, d, free);
  }
  // Compare rates: Use in the quote. The vendor for every part its
  // rates have: the card's part of that name, else a new one (at most MAX_SOURCES), their carrier with it; their costs
  // then go in as when the vendor is picked by hand (follow: another vendor's untouched lines go first)
  function useVendor(vendor, label = null) {
    const d = card && view === "card" ? R.ratesForQuote(rates().best, vendor, cur() || {}, optsOn() ? label : null) : null;
    if (!d || !vendorKeys().includes(vendor)) return false;
    const lab = optsOn() ? String(d.label || "") : null;     // the option Compare rates' column is
    let did = false;
    for (const seg of [...new Set((d.lines || []).map(l => l.seg))]) {
      let s = card.sources.find(x => x.seg === seg);
      if (!s) {
        // the card's one empty part (nobody picked yet) takes it, else a new part
        const blank = card.sources.length === 1 && !card.sources[0].key ? card.sources[0] : null;
        if (blank && !(d.lines || []).some(l => l.seg === blank.seg)) { blank.seg = seg; s = blank; }
        else if (card.sources.length < Q.MAX_SOURCES) { s = Q.source(seg); card.sources.push(s); }
        else continue;
      }
      if (s.key !== vendor || (lab !== null && s.label !== lab)) { s.key = vendor; s.name = ""; s.label = lab; if (d.carrier) s.carrier = String(d.carrier).slice(0, 40); did = true; }
    }
    card.sources.forEach((s, k) => { if (s.key === vendor) follow(k); });
    if (did) changed();
    return did;
  }
  function fillFrom(i) {
    const s = card.sources[i]; if (!s) return;
    const d = pick(s); if (!d) return;
    if (d.cur !== card.cur) { footErr(`Their rates are in ${esc(d.cur)} and this quote is in ${esc(card.cur)}. Change the currency first, or type the costs.`); return; }
    if (s.filled && s.filled !== filledKey(d, s)) unlay(s);     // another vendor's untouched lines go first
    if (!lay(s, d)) return;
    changed(); keepScroll(paint); footErr("");
  }
  function costHtml() {
    const c = cur() || {};
    return `<section class="qc-cost" aria-label="Our cost"><h3>Our cost <span>internal, never in the mail or the PDF</span></h3>
      <p class="muted small m0">Type each line's cost from the vendor's mail; the rate to the requester is the cost plus the markup. A line with no cost is our own fee.</p>
      <div class="mk"><label>Markup on the costs <input type="text" inputmode="decimal" id="qc-pct" value="${esc(card.pct)}" aria-label="Markup percent"> %</label>
        <span class="muted small">${Q.isOffice(c) ? "The requester is one of our offices: no markup." : `OppList's rule${Number.isFinite(Number(S.meta?.quoteRules?.markupPct)) ? ` (${esc(S.meta.quoteRules.markupPct)}%)` : ""}; change any line's markup by hand.`}</span></div>
      <div id="qc-sum">${sumHtml()}</div></section>`;
  }
  function sumHtml() {
    const T = Q.totals(card), warns = Q.warnings(card, rules()), neg = T.profit < 0 ? " neg" : "";
    return `<div class="sum"><div><span>Cost</span><b>${esc(card.cur)} ${Q.money(T.cost)}</b></div><div><span>Sell</span><b>${esc(card.cur)} ${Q.money(T.t)}</b></div>
      <div class="${neg}"><span>Profit</span><b>${esc(card.cur)} ${Q.money(T.profit)}</b></div><div class="${neg}"><span>Margin</span><b>${T.t ? `${T.margin.toFixed(1)}%` : "-"}</b></div></div>
      ${warns.length ? `<div class="warn2" role="status"><ul>${warns.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}`;
  }
  // every figure moves the totals, the computed rates and the amounts shown, without redrawing what is being typed
  function refresh() {
    const T = Q.totals(card);
    if (q("qc-total")) q("qc-total").textContent = `${card.cur} ${Q.money(T.t)}`;
    if (q("qc-atcost")) q("qc-atcost").textContent = T.atCost ? "plus the AT COST lines" : "";
    if (q("qc-sum")) q("qc-sum").innerHTML = sumHtml();
    if (q("qc-count")) q("qc-count").textContent = `· ${plural(Q.used(card).length, "line")}`;
    dr.querySelectorAll("#qc-ls .ql").forEach(el => {
      const l = card.lines[Number(el.dataset.i)]; if (!l) return;
      const a = Q.amountOf(l, card.costs), r = Q.rateOf(l, card.costs), ro = el.querySelector(".ro"), amt = el.querySelector('[data-f="amount"]');
      if (ro) ro.textContent = r?.n !== undefined ? Q.rateText(r.n) : "";
      if (amt) amt.placeholder = a && !a.hand ? (a.n !== undefined ? Q.money(a.n) : a.w) : "";
      const notes = el.querySelector(".notes"); if (notes) notes.innerHTML = notesOf(l);
      el.classList.toggle("miss", !!(tried && String(l.charge || "").trim() && !a));
    });
  }
  // a redraw that keeps the cursor where it was (a cost turns a typed rate into a computed one)
  function repaintKeep(el) {
    const i = el.closest(".ql")?.dataset.i, f = el.dataset.f, pos = el.selectionStart, top = dr.querySelector(".dr-body").scrollTop;
    paint(); dr.querySelector(".dr-body").scrollTop = top;
    const back = dr.querySelector(`.ql[data-i="${i}"] [data-f="${f}"]`);
    if (back) { back.focus(); try { back.setSelectionRange(pos, pos); } catch { /* not a text box */ } }
  }

  // ---- step 2: the mail, as it will read
  function mailHtml() {
    const c = cur() || {}, r = rep(), probs = tried ? probsNow() : probsNow().filter(p => !/^Write the To/.test(p)), warns = Q.warnings(card, rules());
    const n = nextN(), who = r.name ? `${esc(r.name)} <span>· ${esc(c.req || "")}</span>` : esc(c.req || "the requester");
    // why it is a new mail when it is one (OppList's via; the site's own vendor guard)
    const how = r.via === "colleague" ? `This case came in through a colleague's mail, so Make Outlook drafts writes a new mail to ${r.first ? esc(r.first) : "the requester"} (it never answers our own mail).`
      : r.via === "forward" ? "A colleague forwarded their mail, so Make Outlook drafts writes a new mail to the original sender, with the colleague on CC."
      : r.dropped?.length ? "A vendor of this case is on their mail, so Make Outlook drafts writes a new mail (no Reply All), and the vendor is not on it."
      : r.mid ? `Make Outlook drafts replies to ${r.first ? `${esc(r.first)}'s` : "their"} mail of ${esc(day(r.on, true))} when it is in your Outlook (the thread stays together, their mail below). If it is not there, it writes a new mail to these addresses.`
      : `Make Outlook drafts writes a new mail to these addresses (their original mail is not known on this case).`;
    return `${probs.length ? `<div class="problems" role="status"><b>Before you make the draft</b><ul>${probs.map(p => `<li>${esc(p)}</li>`).join("")}</ul><button type="button" class="linkbtn small" data-go="1">Back to the quote</button></div>` : ""}
      ${warns.length ? `<div class="warn2" role="status"><ul>${warns.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
      <div class="ask-mc qc-mc">
        <div class="mc-bar"><span class="who">Reply to ${who}</span><button type="button" class="linkbtn small" id="qc-pv">Preview the PDF</button></div>
        <p class="qc-how small">${how}${card.costs ? " The costs and the markup are never in the mail or the PDF." : ""} <button type="button" class="linkbtn small" id="qc-helper">What is Make Outlook drafts?</button></p>
        <label class="mc-row"><span class="k">To</span><input type="text" id="qc-to" value="${esc(card.to)}" maxlength="1000" aria-label="To" autocomplete="off" spellcheck="false"></label>
        <div class="mc-row"><span class="k">CC</span><input type="text" id="qc-cc" value="${esc(card.cc)}" maxlength="1000" aria-label="CC" placeholder="none" autocomplete="off" spellcheck="false">${teamAddr() ? `<label class="chk"><input type="checkbox" id="qc-team"${card.teamCc ? " checked" : ""}> ${esc(nameOf(S.meta.teamCc))}</label>` : ""}</div>
        <label class="mc-row"><span class="k">Subject</span><input type="text" id="qc-subject" value="${esc(card.subject)}" maxlength="300" aria-label="Subject"></label>
        <div class="mc-row"><span class="k">Attached</span><span class="v">${card.attach ? `<b>${esc(Q.pdfName(opp, n))}</b> <span class="muted small">· the quotation ·</span> <button type="button" class="linkbtn small" id="qc-pv2">Preview</button>` : '<span class="muted">No attachment: the quote is only in the mail</span>'}</span><label class="chk"><input type="checkbox" id="qc-att"${card.attach ? " checked" : ""}> Attach the PDF</label></div>
        <div class="mc-body">
          <div class="greet">${esc(Q.greetOf(c))}<span class="per">${r.first ? "from their mail" : "no first name on their mail"}</span></div>
          <textarea id="qc-open" rows="2" maxlength="1000" aria-label="Opening">${esc(card.opening)}</textarea>
          <div class="qt-wrap">${tableHtml(Q.table(card))}</div>
          <div class="qc-after">${afterHtml()}</div>
          <textarea id="qc-close" rows="2" maxlength="1000" aria-label="Closing">${esc(card.closing)}</textarea>
          <div class="mc-sig">Your Outlook signature goes here.</div>
          ${r.mid ? `<div class="mc-orig">Under it, as in any reply: ${r.first ? `${esc(r.first)}'s` : "their"} mail of ${esc(day(r.on, true))}.</div>` : ""}
        </div>
      </div>
      <details class="ask-alt qc-alt"><summary>No Make Outlook drafts on this computer? Reply in Outlook yourself</summary>
        <div class="alt-in">
          <p class="m0">${r.mid ? `Open ${r.first ? `${esc(r.first)}'s` : "their"} mail in Outlook, click Reply All, change the subject and paste the quote (the table stays a table); attach the PDF if you want it.` : "Write a new mail in Outlook to the addresses above, copy the subject and paste the quote (the table stays a table); attach the PDF if you want it."}</p>
          <div class="btns"><button type="button" class="btn small" id="qc-copy">Copy the quote</button><button type="button" class="btn small" id="qc-subj">Copy the subject</button><button type="button" class="btn small" id="qc-dl">Download the PDF</button></div>
          <div class="ask-logrow"><span class="small">After you send it:</span><label class="field">Sent on<input type="date" id="qc-logdate" value="${todayIso()}" max="${todayIso()}"></label><button type="button" class="btn" id="qc-logit">Log quote sent</button></div>
          <div class="err" id="qc-alterr" role="alert"></div>
        </div></details>`;
  }
  function tableHtml(t) {
    const td = (v, i, tag = "td") => `<${tag}${t.align[i] === "r" ? ' class="r"' : ""}>${esc(v)}</${tag}>`;
    return `<table class="qt"><thead><tr>${t.head.map((h, i) => td(h, i, "th")).join("")}</tr></thead><tbody>${t.rows.map(r => `<tr>${r.map((v, i) => td(v, i)).join("")}</tr>`).join("")}`
      + `<tr class="tot">${t.foot.map((v, i) => td(v, i)).join("")}</tr></tbody></table>${t.note ? `<p class="tl2">${esc(t.note)}</p>` : ""}`;
  }
  function afterHtml() {
    const tt = Q.termsOf(card);
    return `<p class="tl2">Valid until: ${esc(Q.longDay(card.valid) || "[date]")}${card.transit.trim() ? `<br>Transit time: ${esc(card.transit.trim())}` : ""}${card.routing.trim() ? `<br>Routing: ${esc(card.routing.trim())}` : ""}</p>`
      + `${card.other.trim() ? `<p class="tl2">Remarks: ${esc(card.other.trim())}</p>` : ""}${tt.length ? `<div class="tl2">Terms:<ol>${tt.map(x => `<li>${esc(x)}</li>`).join("")}</ol></div>` : ""}`;
  }

  // ---- Just log it (the Log quote sent form lives in the card)
  function justHtml() {
    const c = cur() || {}, today = todayIso();
    return `<div class="qc-just"><p class="muted small m0">For a quote sent another way (our Excel quotation, a phone call). The same as the Log quote sent form before the card.</p>
      <div class="row2"><label class="field">Date sent<input type="date" id="qc-jl-date" value="${today}" max="${today}"></label>
        <label class="field">Sent by<select id="qc-jl-by">${ownerOptions(myOwner() || c.owner || "", "Pick who sent it")}</select></label></div>
      <label class="field">What was quoted (optional)<textarea id="qc-jl-what" rows="3" maxlength="1000" placeholder="e.g. 1x40HQ port to port, valid to Nov 15"></textarea></label>
      ${sourcesHtml()}
      <p class="hint small">Prices stay in the email. A quote made with the card is kept on the case instead.</p></div>`;
  }

  // ---- done
  function doneHtml() {
    const c = cur() || {}, r = rep(), d = done || {};
    const kept = d.logged ? `<div class="kept">Saved on the case as <b>Quote ${d.n}</b>: the lines and the sell prices for everyone (${esc(d.cur)} ${Q.money(d.total)})${d.costs ? `, the costs for people with Costs access (cost ${esc(d.cur)} ${Q.money(d.cost)}, margin ${d.margin.toFixed(1)}%)` : ""}.
      <q>${esc(d.what)}</q>The case shows Quoted after the next update.</div>` : "";
    if (d.how === "draft") return `<div class="okbox" role="status"><h3>Copied for Outlook</h3>
        <ol><li>Double-click <b>Make Outlook drafts</b> on your desktop.</li>
          <li>${r.mid ? `It makes a Reply All under ${r.first ? `${esc(r.first)}'s` : "their"} mail of ${esc(day(r.on, true))}` : "It makes a new mail to the card's addresses"}, with the table${d.pdf ? ` and the PDF attached (the site saved <b>${esc(d.pdf)}</b> to your Downloads; after attaching it, Make Outlook drafts moves it to Documents › JoJo quotes)` : ""}, in <b>Drafts › OppList</b>.</li>
          <li>Check the quote and click Send.</li></ol>
        ${d.pdfFailed ? '<p class="after err">The PDF could not be made. The draft has no attachment; use Download the PDF after opening the card again, or send without it.</p>' : ""}
        <p class="after">${d.logged ? `Logged as quote sent today by ${esc(d.by)}.` : "Not logged: after you send it, open the card again and use Just log it."}</p></div>
      ${kept}<p class="small m0"><button type="button" class="linkbtn small" id="qc-what">See what was copied for Make Outlook drafts</button></p>`;
    return `<div class="okbox" role="status"><h3>Logged</h3><p class="after">Quote sent ${esc(day(d.date, true))} by ${esc(d.by)}. The case shows Quoted after the next update.</p></div>
      ${d.how === "hand" ? kept : `<div class="kept">What the case keeps:<q>${esc(d.what || "Logged by hand")}</q></div>`}`;
  }

  // ------------------------------------------------------------------ moving between steps
  function go(k) {
    if (view !== "card" || k === step) return;
    if (k === 2) tried = true;
    step = k; paint();
  }

  // ------------------------------------------------------------------ events
  function wire() {
    dr.addEventListener("input", ev => {
      const el = ev.target;
      if (el.dataset.sf === "name" || el.dataset.sf === "carrier") { const s = card.sources[Number(el.closest(".src").dataset.s)]; if (s) { s[el.dataset.sf] = el.value; changed(); } return; }
      if (el.dataset.f) {
        const i = Number(el.closest(".ql").dataset.i), f = el.dataset.f, l = card.lines[i]; if (!l) return;
        const before = Q.num(l.cost) !== null;
        l[f] = el.value;
        if (f === "cost" && card.costs && (Q.num(el.value) !== null) !== before) {
          if (Q.num(el.value) !== null && !String(l.markup || "").trim()) l.markup = Q.markupFor(el.value, card.pct);
          changed(); repaintKeep(el); return;
        }
        changed(); refresh(); return;
      }
      if (el.id === "qc-pct") {
        card.pct = Q.num(el.value) ?? 0;
        for (const l of card.lines) if (Q.num(l.cost) !== null) l.markup = Q.markupFor(l.cost, card.pct);
        dr.querySelectorAll('#qc-ls [data-f="markup"]').forEach(x => { const l = card.lines[Number(x.closest(".ql").dataset.i)]; if (l) x.value = l.markup; });
        changed(); refresh(); return;
      }
      const map = { "qc-transit": "transit", "qc-routing": "routing", "qc-other": "other", "qc-to": "to", "qc-cc": "cc", "qc-subject": "subject", "qc-open": "opening", "qc-close": "closing" };
      if (map[el.id]) {
        card[map[el.id]] = el.value; changed();
        if (el.tagName === "TEXTAREA") fit(el);
        if (["qc-transit", "qc-routing", "qc-other"].includes(el.id)) return;
        if (el.id === "qc-to" || el.id === "qc-cc") { const b = q("qc-make"); if (b) b.disabled = probsNow().length > 0; }
      }
    });
    dr.addEventListener("change", ev => {
      const el = ev.target;
      if (el.dataset.sf === "seg" || el.dataset.sf === "key" || el.dataset.sf === "label") {
        const k = Number(el.closest(".src").dataset.s), s = card.sources[k]; if (!s) return;
        s[el.dataset.sf] = el.value;
        if (el.dataset.sf === "key" && el.value !== "other") s.name = "";
        // another vendor starts on its lowest option (named on the part, so the quote says which)
        if (el.dataset.sf === "key") { s.label = null; if (optsOn()) { const d = pick(s); s.label = d ? String(d.label || "") : null; } }
        follow(k);                                     // their saved rates go in at once, the last ones out
        changed(); keepScroll(paint);
        const back = el.dataset.sf === "key" && el.value === "other" ? "name" : el.dataset.sf;
        dr.querySelector(`.src[data-s="${k}"] [data-sf="${back}"]`)?.focus();
        return;
      }
      if (el.id === "qc-cur") { card.cur = el.value; changed(); paint(); return; }
      if (el.id === "qc-valid") { card.valid = validIso(el.value) ? el.value : ""; changed(); return; }
      if (el.dataset.term) { card.terms[el.dataset.term] = el.checked; card.termsOpen = true; changed(); const s = dr.querySelector("#qc-terms .c"); if (s) s.textContent = `· ${Q.TERMS.filter(x => card.terms[x.k]).length} of ${Q.TERMS.length} ticked, the company's standard lines`; return; }
      if (el.dataset.f === "unit") { const l = card.lines[Number(el.closest(".ql").dataset.i)]; if (l) { l.unit = Q.unitText(el.value); el.value = l.unit; changed(); } return; }
      if (el.dataset.f === "charge") { const l = card.lines[Number(el.closest(".ql").dataset.i)]; if (l) { l.charge = el.value.trim().toUpperCase(); el.value = l.charge; changed(); } return; }
      if (el.id === "qc-team") { card.teamCc = el.checked; setPrefTeam(el.checked); changed(); return; }
      if (el.id === "qc-att") { card.attach = el.checked; changed(); paint(); return; }
      if (el.id === "qc-log") { card.logIt = el.checked; changed(); return; }
      if (el.id === "qc-by") { card.sentBy = el.value; changed(); return; }
    });
    dr.addEventListener("toggle", ev => { if (ev.target.id === "qc-terms" && card) card.termsOpen = ev.target.open; }, true);
    dr.addEventListener("click", async ev => {
      const t = ev.target.closest("button"); if (!t || t.disabled) return;
      if (t.dataset.qcX !== undefined || t.id === "qc-done") return close();
      if (t.dataset.step) return go(Number(t.dataset.step));
      if (t.dataset.go) return go(Number(t.dataset.go));
      if (t.dataset.rm !== undefined) { card.lines.splice(Number(t.dataset.rm), 1); if (!card.lines.length) card.lines.push(Q.line("", "1", "")); changed(); keepScroll(paint); return; }
      if (t.dataset.open !== undefined) { card.lines[Number(t.dataset.open)].open = true; keepScroll(paint); dr.querySelector(`.ql[data-i="${t.dataset.open}"] [data-f="remark"]`)?.focus(); return; }
      if (t.dataset.chip) { const [n, u, r] = t.dataset.chip.split("|"); card.lines.push(Q.line(n, r ? "" : Q.qtyFor(cur(), u), u, r || "")); changed(); keepScroll(paint); return; }
      if (t.id === "qc-add") { card.lines.push(Q.line("", "1", "")); changed(); keepScroll(paint); dr.querySelector(`.ql[data-i="${card.lines.length - 1}"] [data-f="charge"]`)?.focus(); return; }
      if (t.dataset.days) { const today = todayIso(); card.valid = t.dataset.days === "end" ? Q.monthEnd(today) : Q.addDays(today, Number(t.dataset.days)); q("qc-valid").value = card.valid; q("qc-valid").removeAttribute("aria-invalid"); changed(); return; }
      if (t.id === "qc-reset") { if (await confirmBox("Start again", "Start the card again from the case? The lines, the costs and the dates you typed go.", "Start again")) { card = fresh(cur(), true); keep.del(DRAFT(opp)); tried = false; dirty = false; step = 1; paint(); } return; }
      if (t.id === "qc-just") { view = "just"; paint(); return; }
      if (t.dataset.srcFill !== undefined) return fillFrom(Number(t.dataset.srcFill));
      if (t.dataset.srcCheck !== undefined) { onCheck?.(t.dataset.srcCheck); return; }   // the card is kept in this browser
      if (t.id === "qc-src-add") { card.sources.push(Q.source(card.sources.some(s => s.seg === "Freight") ? "Inland" : "Freight")); changed(); keepScroll(paint); return; }
      if (t.dataset.srcRm !== undefined) { unlay(card.sources[Number(t.dataset.srcRm)]); card.sources.splice(Number(t.dataset.srcRm), 1); if (!card.sources.length) card.sources.push(Q.source("Freight")); changed(); keepScroll(paint); return; }
      if (t.id === "qc-helper") return helperBox();
      if (t.id === "qc-back-card") { view = "card"; paint(); return; }
      if (t.id === "qc-jl-save") return justLog(t);
      if (t.id === "qc-pv" || t.id === "qc-pv2") return preview(t);
      if (t.id === "qc-copy") return copyQuote(t);
      if (t.id === "qc-subj") return copyPlainBtn(t, card.subject);
      if (t.id === "qc-dl") return downloadByHand(t);
      if (t.id === "qc-logit") return logByHand(t);
      if (t.id === "qc-make") return make(t);
      if (t.id === "qc-again") { try { await copyText(Q.quoteClip(done.req)); flashBtn(t, "Copied again (same request)"); } catch { footErr("Could not copy. Close and use Reply in Outlook yourself."); } return; }
      if (t.id === "qc-what") return showCopied();
    });
    scrim.addEventListener("click", close);
  }
  function keepScroll(fn) { const body = dr.querySelector(".dr-body"), top = body.scrollTop; fn(); body.scrollTop = top; }

  // ------------------------------------------------------------------ the PDF
  const sheet = n => Q.sheetOf(card, cur() || {}, { opp, n, today: todayIso(), me: { name: S.me?.name || "", email: S.me?.email || "" } });
  async function preview(btn) {
    const was = btn.textContent; btn.disabled = true; btn.textContent = "Making the PDF…";
    try { const blob = await quotePdf(sheet(nextN())); if (!openBlob(blob)) footErr("The browser blocked the new tab. Allow pop-ups for this site, or use Download the PDF."); }
    catch (e) { footErr(`Could not make the PDF. ${failHtml(e, { part: `${opp} quote PDF` })}`); }
    finally { btn.disabled = false; btn.textContent = was; }
  }
  async function downloadByHand(btn) {
    const was = btn.textContent, n = nextN(), err = q("qc-alterr"); err.textContent = "";
    btn.disabled = true; btn.textContent = "Making the PDF…";
    try { downloadBlob(await quotePdf(sheet(n)), Q.pdfName(opp, n)); handPdf = Q.pdfName(opp, n); flashBtn(btn, "Downloaded", was); }
    catch (e) { btn.disabled = false; btn.textContent = was; err.innerHTML = `Could not make the PDF. ${failHtml(e, { part: `${opp} quote PDF` })}`; }
  }

  // ------------------------------------------------------------------ copying
  async function copyQuote(btn) {
    try { await copyRich(Q.outlookHtml(card, cur() || {}), Q.plainText(card, cur() || {})); flashBtn(btn, "Copied"); }
    catch { q("qc-alterr").textContent = "Could not copy. Select the text and press Ctrl+C."; }
  }
  async function copyPlainBtn(btn, text) {
    try { await copyText(text); flashBtn(btn, "Copied"); }
    catch { q("qc-alterr").textContent = "Could not copy. Select the text and press Ctrl+C."; }
  }
  function showCopied() {
    const clip = Q.quoteClip(done.req), i = clip.indexOf("\n");
    modal("What Make Outlook drafts gets", `<p class="muted small m0">The clipboard: a line for people, then the request. Only the sell prices, no costs; the PDF itself is not in it, only its file name.</p>
      <pre class="qc-pre">${esc(clip.slice(0, i))}</pre><pre class="qc-pre">${esc(JSON.stringify(JSON.parse(clip.slice(i + 1)), null, 2))}</pre>`, { wide: true });
  }

  // ------------------------------------------------------------------ saving the quote
  function check(needTo) {
    tried = true;
    const probs = Q.problems(card, { needTo, today: todayIso() });
    if (probs.length) { step = 2; paint(); return false; }
    if (!sentBy()) { footErr("Pick who sends the quote (Sent by)."); q("qc-by")?.focus(); return false; }
    return true;
  }
  // the quote action (date, what, sentBy) with quoteId and rateFrom, written with quotes/{id} and, when
  // a cost was typed, quoteCosts/{id}; one id for the request, the action and the documents
  async function save(date, how, n, pdf) {
    if (!reqId) reqId = newActionId();
    const T = Q.totals(card), withCosts = card.costs && Q.used(card).some(l => Q.num(l.cost) !== null), what = Q.whatOf(card, n);
    const sources = Q.sourcesOf(card), rateFrom = Q.rateFromOf(sources);   // whose rates, with the quote
    await act("quote", opp, { date, what, sentBy: sentBy(), quoteId: reqId, rateFrom, sources },
      { id: reqId, quote: Q.quoteDoc(card, { opp, n, date, how, pdf, rateFrom, sources }), costs: withCosts ? Q.costDoc(card, { opp }) : null });
    keep.del(DRAFT(opp)); dirty = false;
    return { n, what, total: T.t, cost: T.cost, margin: T.margin, costs: withCosts, cur: card.cur };
  }
  async function make(btn) {
    footErr("");
    if (!check(!rep().mid)) return;
    const n = nextN(), label = btn.textContent;
    if (!reqId) reqId = newActionId();
    const req = Q.quoteRequest({ id: reqId, opp, card, c: cur() || {}, teamAddress: teamAddr(), n, mid: rep().mid });
    btn.disabled = true; btn.textContent = "Copying…";
    // the clipboard first, while the click still counts as the person's (browsers refuse it later)
    try { await copyText(Q.quoteClip(req)); }
    catch { btn.disabled = false; btn.textContent = label; footErr("Could not copy the mail for Outlook. Use Reply in Outlook yourself, below."); return; }
    let pdf = "", pdfFailed = false;
    if (card.attach) {
      btn.textContent = "Making the PDF…";
      try { downloadBlob(await quotePdf(sheet(n)), Q.pdfName(opp, n)); pdf = Q.pdfName(opp, n); } catch { pdfFailed = true; }
    }
    let saved = null;
    if (card.logIt) {
      btn.textContent = "Saving the quote…";
      try { saved = await save(todayIso(), "draft", n, pdf); }
      catch (e) {
        btn.disabled = false; btn.textContent = label;
        footErr(`The mail is copied for Outlook${pdf ? " and the PDF is in your Downloads" : ""}, but the quote is not saved on the case yet. Press the button again. ${failHtml(e, { part: opp })}`);
        return;
      }
    }
    done = { how: "draft", n, pdf, pdfFailed, logged: !!saved, req, by: sentBy(), date: todayIso(), ...(saved || {}) };
    view = "done"; paint();
    if (saved) onDone?.("quoted", n);
  }
  async function logByHand(btn) {
    const err = q("qc-alterr"); err.textContent = "";
    const date = q("qc-logdate").value;
    if (!validIso(date) || date > todayIso()) { err.textContent = "Pick the day the quote went out, up to today."; return; }
    if (!check(false)) return;
    const n = nextN();
    if (!await confirmBox("Log quote sent", `Log Quote ${n} as sent on ${day(date)}? Do this after the mail is sent.`, "Log quote sent")) return;
    btn.disabled = true;
    try {
      const saved = await save(date, "hand", n, handPdf === Q.pdfName(opp, n) ? handPdf : "");
      done = { how: "hand", logged: true, by: sentBy(), date, ...saved };
      view = "done"; paint(); onDone?.("quoted", n);
    } catch (e) { btn.disabled = false; err.innerHTML = failHtml(e, { part: opp }); }
  }
  async function justLog(btn) {
    footErr("");
    const date = q("qc-jl-date").value, by = q("qc-jl-by").value, what = q("qc-jl-what").value.trim();
    if (!validIso(date) || date > todayIso()) { footErr("Pick a date up to today."); return; }
    if (!by) { footErr("Pick who sent the quote."); return; }
    btn.disabled = true;
    try {
      const sources = Q.sourcesOf(card);   // whose rates, on the action (no quotes/ document for Just log it)
      await act("quote", opp, { date, what, sentBy: by, rateFrom: Q.rateFromOf(sources), sources });
      done = { how: "just", by, date, what };
      view = "done"; paint(); onDone?.("logged");
    } catch (e) { btn.disabled = false; footErr(failHtml(e, { part: opp })); }
  }
  function flashBtn(btn, msg, was = btn.textContent) {
    btn.textContent = msg; btn.disabled = true;
    setTimeout(() => { if (btn.isConnected) { btn.textContent = was; btn.disabled = false; } }, 1600);
  }

  return {
    show, isOpen: () => open,
    toggle() { open ? close() : show(); },
    // a sync arrived: the head line follows the case (nothing being typed is redrawn)
    refresh() { if (open && dr && card) dr.querySelector(".dr-sub").textContent = [opp, Q.modeOf(cur() || {}), endsText(cur() || {}), Q.cargoPhrase(cur() || {}), Q.incotermOf(cur() || {})].filter(Boolean).join(" · "); },
    leave() { hide(); },
  };
}

// the clipboard, with the old way as a fallback (some browsers refuse the new one outside a click)
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return; } catch { /* fall back */ }
  const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  const ok = document.execCommand("copy"); ta.remove();
  if (!ok) throw new Error("copy refused");
}
// the quote with its table as a table (Outlook keeps it), with plain text beside it
async function copyRich(html, text) {
  try { await navigator.clipboard.write([new ClipboardItem({ "text/html": new Blob([html], { type: "text/html" }), "text/plain": new Blob([text], { type: "text/plain" }) })]); }
  catch { await copyText(text); }
}

// a saved quote, opened from the case's Quotes: the lines as quoted, and its costs for the admin and Costs
export function showQuote(quote, costs) {
  const t = { head: ["Charge", "Qty", "Unit", `Rate (${quote.cur})`, `Amount (${quote.cur})`, "Remarks"], align: ["l", "r", "l", "r", "r", "l"] };
  const rows = (quote.lines || []).map((l, i) => {
    const k = costs?.lines?.[i];
    return `<tr><td>${esc(l.charge)}</td><td class="r">${esc(l.qty)}</td><td>${l.unit ? `PER ${esc(l.unit)}` : ""}</td><td class="r">${l.rate !== null && l.rate !== undefined ? Q.rateText(l.rate) : esc(l.word)}</td>`
      + `<td class="r">${l.amount !== null && l.amount !== undefined ? Q.money(l.amount) : esc(l.word)}</td><td>${esc(l.remark)}</td>`
      + (costs ? `<td class="r">${k && k.cost !== null && k.cost !== undefined ? Q.rateText(k.cost) : '<span class="muted">-</span>'}</td><td class="r">${k && k.markup !== null && k.markup !== undefined ? Q.rateText(k.markup) : ""}</td>` : "") + "</tr>";
  }).join("");
  const head = [...t.head, ...(costs ? ["Cost", "Markup"] : [])];
  modal(`Quote ${quote.n} · ${quote.opp}`, `<p class="muted small m0">${esc(day(quote.date, true))} · ${esc(quote.name || quote.by || "")}${quote.how === "draft" ? " · made with Create draft" : quote.how === "hand" ? " · sent by hand" : ""}${quote.pdf ? ` · ${esc(quote.pdf)}` : ""}</p>
    <div class="tablewrap"><table class="grid qc-view"><thead><tr>${head.map((h, i) => `<th${[1, 3, 4, 6, 7].includes(i) ? ' class="r"' : ""}>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows}
      <tr class="tot"><td>TOTAL</td><td></td><td></td><td></td><td class="r">${Q.money(quote.total)}</td><td>${quote.atCost ? "Plus the AT COST lines" : ""}</td>${costs ? `<td class="r">${Q.money(costs.cost)}</td><td></td>` : ""}</tr></tbody></table></div>
    <p class="m0">Valid until ${esc(Q.longDay(quote.valid))}${quote.transit ? ` · transit ${esc(quote.transit)}` : ""}${quote.routing ? ` · ${esc(quote.routing)}` : ""}</p>
    ${quote.remarks ? `<p class="m0">Remarks: ${esc(quote.remarks)}</p>` : ""}
    ${quote.sources?.length ? `<p class="m0">Rates from ${esc(Q.sourcesText(quote.sources))} <span class="muted small">(for the case, not in the mail)</span></p>` : ""}
    ${costs ? `<p class="m0 qc-costline">Cost ${esc(quote.cur)} ${Q.money(costs.cost)} · profit ${esc(quote.cur)} ${Q.money(costs.profit)} · margin ${Number(costs.margin || 0).toFixed(1)}% <span class="muted small">(only people with Costs access see this line)</span></p>` : ""}`, { wide: true });
}
