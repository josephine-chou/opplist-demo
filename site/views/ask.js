// Ask vendors: a drawer that slides in from the right, in three steps, opened from a
// case's Rate requests. The case stays where it is on the left.
//   1 Vendors: the list OppList found in the mail (lists/vendors), grouped for this case's countries; the first time a
//     case is opened, the ones that quoted there before are ticked (up to five). A vendor with no address on file
//     gets a box for it when ticked; an address not on the list can be typed.
//   2 Mail: one message for all, shown as the mail itself; the greeting and the addresses change with the vendor.
//   3 Drafts: Create drafts puts the mails on the clipboard for Make Outlook drafts (the helper on the computer, one
//     Outlook draft each, in Drafts > OppList) and logs them on the case as asked today. By hand: copy
//     each part into Outlook, then Log as sent. Either way it is an action of type ask, which
//     OppList takes in at its next sync. Then this round is done; the next one starts when the drawer opens again.
// The card (who is ticked, the subject, the message, addresses typed) is kept in cards/{OPP} for everyone and for the
// next round. The case's live updates redraw only the vendor list, and not while an address is being typed there.
import { S, $, confirmBox, modal } from "../core.js";
import { esc, day, todayIso, validIso, stamp, norm, plural, nowMs } from "../lib.js";
import { act, getCard, saveCard } from "../data.js";
import { failHtml } from "../issues.js";
import { helperBox } from "./common.js";
import { subjectOf, bodyOf, mailOf, missingOf, pretick, farCountries, worksIn, quotedFor, greeting, greetingWord, endsOf,
  addressOf, nameOf, cleanList, draftsRequest, draftsClip, MAX_DRAFTS, parseAddresses, byCompany, domainOf, isFreeMail } from "../askmail.js";

const PREF = "opplist.ask.teamCc";           // the person's last choice for CC #JJC-MFN (ticked at first)
const prefTeam = () => { try { return localStorage.getItem(PREF) !== "0"; } catch { return true; } };
const setPrefTeam = on => { try { localStorage.setItem(PREF, on ? "1" : "0"); } catch { /* private window */ } };
// a box takes several addresses; the typed ones are one mail per company (askmail.byCompany)
const MAX_ADHOC = 10, MAX_BODY = 5000, MAX_PER_MAIL = 10;
const STEPS = ["Vendors", "Mail", "Drafts"];

export function askPanel(opp, cur, { onLogged } = {}) {
  let open = false, card = null, saved = null, saveT = 0, dirty = false;
  let step = 1, filter = { q: "", all: false }, pv = 0, preticked = false;
  let round = null;        // once Create drafts or Log as sent is done: { how, date, subject, mails, req }
  let reqId = "";          // the request's id: the same when the same drafts are copied again
  let dr = null, scrim = null;
  const q = id => dr?.querySelector(`#${id}`) || null;

  const vendors = () => S.vendors?.rows || [];
  const byKey = () => new Map(vendors().map(v => [v.key, v]));
  const team = () => S.meta?.teamCc || "";
  const teamAddr = () => cleanList([team()])[0] || "";

  // ------------------------------------------------------------------ opening and closing
  async function show() {
    if (open) return; open = true;
    scrim = document.createElement("div"); scrim.className = "ask-scrim";
    dr = document.createElement("aside"); dr.className = "ask-dr";
    dr.setAttribute("role", "dialog"); dr.setAttribute("aria-modal", "false"); dr.setAttribute("aria-labelledby", "ask-title");
    dr.innerHTML = `<div class="dr-head"><div class="t"><h2 id="ask-title" tabindex="-1">Ask vendors for rates</h2><div class="dr-sub"></div></div>
        <button type="button" class="xbtn" data-ask-x aria-label="Close">&times;</button></div>
      <div class="dr-steps" role="tablist" aria-label="Steps"></div>
      <div class="dr-body"><div class="muted">Loading…</div></div>
      <div class="dr-foot"></div>`;
    document.body.append(scrim, dr);
    wire();
    requestAnimationFrame(() => requestAnimationFrame(() => dr?.classList.add("open")));
    document.addEventListener("keydown", onKey, true);
    try { saved = await getCard(opp); } catch { saved = null; }
    if (!open) return;
    const c = cur(), known = new Set(vendors().map(v => v.key));
    if (saved) {
      card = { vendors: (saved.vendors || []).filter(k => known.has(k)), adhoc: (saved.adhoc || []).map(x => ({ email: x.email, cc: [...(x.cc || [])] })),
        to: { ...(saved.to || {}) }, subject: saved.subject || subjectOf(opp, c), body: saved.body || bodyOf(c), teamCc: prefTeam() };
      preticked = false;
    } else {
      card = { vendors: pretick(vendors(), c), adhoc: [], to: {}, subject: subjectOf(opp, c), body: bodyOf(c), teamCc: prefTeam() };
      preticked = card.vendors.length > 0;
    }
    step = 1; pv = 0; round = null; reqId = ""; filter = { q: "", all: false };
    paint();
    setTimeout(() => q("ask-title")?.focus(), 260);
  }
  function hide() {
    if (!open) return;
    flush(); open = false;
    document.removeEventListener("keydown", onKey, true);
    dr?.remove(); scrim?.remove(); dr = scrim = null;
    card = null; round = null; reqId = "";
  }
  // closed by the person (the cross, Done, Escape, the shade on a phone): the case page redraws its button
  function close() { hide(); onLogged?.("closed"); document.querySelector("[data-ask-open]")?.focus(); }
  // Escape closes the drawer, but not while a box (a confirmation, the helper's explanation) is open over it
  function onKey(ev) { if (ev.key === "Escape" && open && !document.querySelector(".modal")) { ev.preventDefault(); close(); } }

  // ------------------------------------------------------------------ drawing
  function paint() {
    if (!dr || !card) return;
    dr.querySelector(".dr-sub").innerHTML = subLine();
    paintTabs();
    const body = dr.querySelector(".dr-body");
    body.innerHTML = step === 1 ? step1() : step === 2 ? step2() : step3();
    body.scrollTop = 0;
    paintFoot();
    if (step === 2) { paintVendor(); fit(q("ask-body")); }
  }
  function subLine() {
    const ends = endsOf(cur()).filter(Boolean).join(" to ");
    return `${esc(opp)}${ends ? ` · ${esc(ends)}` : ""} · <span id="ask-saved">${savedLine()}</span>`;
  }
  function savedLine() {
    if (dirty) return "Saving…";
    if (saved?.at) return `Saved · ${esc(saved.name || saved.by || "")} · ${esc(stamp(saved.at))}`;
    return "Not saved yet";
  }
  const chosenCount = () => card.vendors.length + card.adhoc.length;
  function paintTabs() {
    const n = chosenCount();
    dr.querySelector(".dr-steps").innerHTML = STEPS.map((label, i) => {
      const k = i + 1, on = step === k, done = round ? k < 3 : k < step;
      const count = k === 1 && n && !round ? ` <span class="c">· ${n}</span>` : "";
      return `<button type="button" role="tab" class="stab${on ? " on" : ""}${done ? " ok" : ""}" data-step="${k}" aria-selected="${on}"${round && !on ? " disabled" : ""}>`
        + `<span class="n" aria-hidden="true">${done ? "&#10003;" : k}</span><span class="lbl">${label}${count}</span></button>`;
    }).join("");
  }
  function paintFoot() {
    const n = chosenCount(), foot = dr.querySelector(".dr-foot");
    if (round) foot.innerHTML = `<span class="info">To ask more vendors, close and open Ask vendors again.</span><span class="grow"></span><button type="button" class="btn primary" id="ask-done">Done</button>`;
    else if (step === 1) foot.innerHTML = `<span class="err" id="f-err" role="alert"></span><span class="info">${n ? `${plural(n, "vendor")} chosen` : "Tick at least one vendor"}</span><span class="grow"></span>`
      + `<button type="button" class="btn primary" data-go="2"${n ? "" : " disabled"}>Next: the mail &rsaquo;</button>`;
    else if (step === 2) foot.innerHTML = `<span class="err" id="f-err" role="alert"></span><button type="button" class="btn" data-go="1">&lsaquo; Back</button><span class="grow"></span>`
      + `<button type="button" class="btn primary" data-go="3">Next: drafts &rsaquo;</button>`;
    else foot.innerHTML = `<span class="err" id="f-err" role="alert"></span><button type="button" class="btn" data-go="2">&lsaquo; Back</button><span class="grow"></span>`
      + `<button type="button" class="btn primary" id="ask-make">Create ${plural(n, "draft")} in Outlook</button>`;
  }
  const footErr = msg => { const e = q("f-err"); if (e) e.textContent = msg; };

  // ---- step 1: vendors
  function step1() {
    const far = farCountries(cur());
    return `<div class="ask-tools">
        ${far.length ? `<div class="seg" role="group" aria-label="Which vendors"><button type="button" data-all="0" aria-pressed="${!filter.all}">${esc(far.join(" and "))}</button><button type="button" data-all="1" aria-pressed="${filter.all}">All countries</button></div>` : ""}
        <input type="search" id="ask-q" placeholder="Search vendors" value="${esc(filter.q)}" aria-label="Search vendors" autocomplete="off">
      </div>
      ${far.length ? "" : `<p class="hint small">This case has no country outside the US yet, so every vendor is listed.</p>`}
      <div id="ask-groups">${groupsHtml()}</div>
      <div class="ask-add"><label for="ask-email">Not on the list? Type the address</label>
        <div class="ask-addline"><input type="text" id="ask-email" placeholder="name@company.example; another@company.example" maxlength="1000" autocomplete="off" spellcheck="false"><button type="button" class="btn" id="ask-add">Add</button></div>
        <p class="hint small">Several at once: put ; or , between them. Addresses at one company go in one mail, the first as To and the others as CC.</p>
        <div class="err" id="ask-adderr" role="alert"></div><div class="hint small" id="ask-addnote" role="status"></div></div>`;
  }
  // the requests already on this case, by vendor (the latest of each)
  function askedHere(c) {
    const m = new Map();
    for (const r of c?.rr || []) { const was = m.get(r.vendor); if (!was || String(r.asked || "") >= String(was.asked || "")) m.set(r.vendor, r); }
    return m;
  }
  function groupsHtml() {
    const c = cur(); if (!c || !card) return "";
    const far = farCountries(c), text = norm(filter.q), rr = askedHere(c), farLabel = far.join(" or ");
    if (!vendors().length && !card.adhoc.length) return `<div class="empty">No vendor list yet. Type the address below.</div>`;
    const on = v => card.vendors.includes(v.key);
    const near = v => far.length > 0 && (worksIn(v, far) || quotedFor(v, far));
    const hit = v => !text || norm(`${v.name} ${v.key} ${v.full || ""} ${v.country || ""} ${(v.askedIn || []).join(" ")}`).includes(text);
    // a search looks in every country; ticked ones stay in view
    const pool = vendors().filter(v => hit(v) && (text || filter.all || !far.length || near(v) || on(v)));
    const byQuoted = (a, b) => (b.quoted || 0) - (a.quoted || 0) || String(b.lastAsked || "").localeCompare(String(a.lastAsked || "")) || a.name.localeCompare(b.name);
    const byAsked = (a, b) => (b.asked || 0) - (a.asked || 0) || a.name.localeCompare(b.name);
    const g1 = pool.filter(v => near(v) && quotedFor(v, far) && !rr.has(v.key)).sort(byQuoted);
    const g2 = pool.filter(v => near(v) && !quotedFor(v, far) && !rr.has(v.key)).sort(byAsked);
    const g3 = pool.filter(v => !near(v) && !rr.has(v.key)).sort(byQuoted);
    const g4 = pool.filter(v => rr.has(v.key)).sort((a, b) => a.name.localeCompare(b.name));
    const grp = (title, hint, rows) => rows.length ? `<div class="ask-group"><h4>${title}${hint ? ` <span>${hint}</span>` : ""}</h4><div class="ask-vl">${rows.map(v => vrow(v, rr.get(v.key))).join("")}</div></div>` : "";
    const typed = card.adhoc.length ? `<div class="ask-group"><h4>Typed in</h4><div class="ask-vl">${card.adhoc.map((x, i) => {
      const at = listedAt(x.email), cc = cleanList(x.cc);
      return `<div class="ask-vr on typed">
        <input type="checkbox" checked disabled aria-label="${esc(x.email)}"><span class="nm">${esc(x.email)}</span>
        <button type="button" class="linkbtn small rm" data-rm="${i}" aria-label="Remove ${esc(x.email)}">Remove</button>
        <span class="why">${at ? `At ${esc(at.name || at.key)}, on the list. The greeting is “${esc(greeting(at.name || at.key, at.country))}”`
          : "Not on the list. The greeting is “Hi team,”"}</span>${cc.length ? `<span class="ad">CC ${esc(cc.join(", "))}</span>` : ""}</div>`;
    }).join("")}</div></div>` : "";
    const none = !pool.length ? `<div class="empty">No vendor${text ? " with that name" : far.length ? ` for ${esc(farLabel)}` : ""}. ${far.length && !filter.all && !text ? "Try All countries, or type" : "Type"} the address below.</div>` : "";
    return grp(`Quoted for ${esc(farLabel)} before`, preticked && g1.some(on) ? "ticked for you" : "", g1)
      + grp(`Also in ${esc(farLabel)}`, "", g2)
      + grp(far.length ? "Other countries" : "Vendors", "", g3)
      + grp("Already asked on this case", "", g4)
      + none + typed;
  }
  function vrow(v, was) {
    const on = card.vendors.includes(v.key), cc = cleanList(v.cc), name = v.name || v.key;
    const ad = v.to ? `<span class="ad">${esc(v.to)}${cc.length ? ` <span class="muted">· CC ${esc(cc.join(", "))}</span>` : ""}</span>`
      : on ? `<span class="ad none">No address on file. <input type="text" data-to="${esc(v.key)}" value="${esc(card.to[v.key] || "")}" placeholder="Type the address (; between several)" maxlength="1000" autocomplete="off" spellcheck="false" aria-label="Address for ${esc(name)}"></span>`
      : `<span class="ad none">No address on file</span>`;
    return `<label class="ask-vr${on ? " on" : ""}"><input type="checkbox" data-v="${esc(v.key)}"${on ? " checked" : ""}>
      <span class="nm">${esc(name)}</span>${v.country ? `<span class="ct">${esc(v.country)}</span>` : "<span></span>"}
      <span class="why">${whyOf(v, was)}</span>${ad}</label>`;
  }
  function whyOf(v, was) {
    if (was) return `<span class="was">Asked on this case ${esc(day(was.asked))} · ${was.pending ? "waiting" : was.quoted ? "quoted" : was.replied ? "replied" : "waiting"}</span>`;
    const last = v.lastAsked ? ` · last asked ${esc(day(v.lastAsked))}` : "";
    if (v.quoted) return `Quoted ${v.quoted} of ${v.asked}${last}`;
    if (v.asked) return `Asked ${v.asked}, no rate yet${last}`;
    return "Not asked yet";
  }
  function paintList() {
    const g = q("ask-groups"); if (!g) return;
    const body = dr.querySelector(".dr-body"), top = body.scrollTop;
    g.innerHTML = groupsHtml(); body.scrollTop = top;
    paintTabs(); paintFoot();
  }

  // ---- step 2: the mail, written where it is read
  function step2() {
    const miss = missingOf(cur());
    return `${miss.length ? `<p class="note-box small" id="ask-miss">Not on this case: ${esc(miss.join(", "))}. Add it to the message if you have it.</p>` : ""}
      <div class="ask-mc">
        <div class="mc-bar"><button type="button" class="pv-nav" data-pv="-1" aria-label="Previous vendor">&lsaquo;</button>
          <span class="who" id="mc-who" aria-live="polite"></span>
          <button type="button" class="pv-nav" data-pv="1" aria-label="Next vendor">&rsaquo;</button></div>
        <div class="mc-row"><span class="k">To</span><span class="v" id="mc-to"></span></div>
        <div class="mc-row"><span class="k">CC</span><span class="v" id="mc-cc"></span>${teamAddr() ? `<label class="chk"><input type="checkbox" id="ask-team"${card.teamCc ? " checked" : ""}> ${esc(nameOf(team()))}</label>` : ""}</div>
        <label class="mc-row"><span class="k">Subject</span><input type="text" id="ask-subject" maxlength="300" value="${esc(card.subject)}"></label>
        <div class="mc-body">
          <div class="greet"><span id="mc-greet"></span><span class="per">set for each vendor</span></div>
          <textarea id="ask-body" maxlength="${MAX_BODY}" spellcheck="true" aria-label="Message">${esc(card.body)}</textarea>
          <div class="mc-sig">Your Outlook signature</div>
        </div>
      </div>
      <p class="hint small">Every vendor gets this message. The greeting and the addresses change with the vendor: use &lsaquo; &rsaquo; to check each one.</p>`;
  }
  // the listed vendor a typed address works at (the same company domain; never a free-mail one), else null
  function listedAt(email) {
    const d = domainOf(email); if (!d || isFreeMail(d)) return null;
    return vendors().find(v => [v.to, ...(v.cc || [])].some(a => domainOf(addressOf(a || "")) === d)) || null;
  }
  // the mails as they stand: one per ticked vendor, then one per typed company. A vendor with no address on file
  // writes to what was typed for it, the first To and the rest CC; a typed company at a listed vendor's domain is
  // greeted with that vendor's name
  function mails() {
    const map = byKey(), extra = card.teamCc && teamAddr() ? [teamAddr()] : [];
    const one = (key, name, greetName, country, to, cc) => {
      const m = { key, name, greetName, country, to: addressOf(to) };
      m.cc = cleanList([...cc, ...extra]).filter(a => a.toLowerCase() !== m.to.toLowerCase());
      m.text = mailOf(card.body, greetName, country);
      return m;
    };
    return [...card.vendors.map(k => {
      const v = map.get(k) || { key: k, name: k, country: "", to: "", cc: [] }, name = v.name || k;
      if (v.to) return one(k, name, name, v.country || "", v.to, v.cc || []);
      const typed = parseAddresses(card.to[k]).good;
      return one(k, name, name, v.country || "", typed[0] || "", [...typed.slice(1), ...(v.cc || [])]);
    }), ...card.adhoc.map(x => { const at = listedAt(x.email); return one("", x.email, at ? at.name || at.key : "", at ? at.country || "" : "", x.email, x.cc || []); })];
  }
  function paintVendor() {
    const ms = mails(); if (pv >= ms.length) pv = 0;
    const m = ms[pv]; if (!m || !q("mc-who")) return;
    q("mc-who").innerHTML = `${esc(m.name)} <span>· ${pv + 1} of ${ms.length}</span>`;
    q("mc-to").textContent = m.to;
    q("mc-cc").textContent = m.cc.join("; ") || "-";
    q("mc-greet").textContent = greeting(m.greetName, m.country);
    const [prev, next] = dr.querySelectorAll(".pv-nav"); prev.disabled = pv === 0; next.disabled = pv >= ms.length - 1;
  }
  function fit(t) { if (!t) return; t.style.height = "auto"; t.style.height = `${Math.max(220, t.scrollHeight + 2)}px`; }

  // ---- step 3: the drafts
  function rowsHtml(ms, withCopy) {
    return `<div class="ask-mails">${ms.map((m, i) => `<div class="ask-mr"><div class="h"><b>${esc(m.name)}</b><span class="muted small">${esc(greetingWord(m.country))}</span></div>
        <div class="l"><span>To</span> ${esc(m.to)}</div><div class="l"><span>CC</span> ${esc(m.cc.join("; ")) || "-"}</div>
        ${withCopy ? `<div class="cp"><button type="button" class="btn small" data-copy="to" data-i="${i}">To</button><button type="button" class="btn small" data-copy="cc" data-i="${i}">CC</button><button type="button" class="btn small" data-copy="subject" data-i="${i}">Subject</button><button type="button" class="btn small primary" data-copy="mail" data-i="${i}">Mail</button></div>` : ""}</div>`).join("")}</div>`;
  }
  function step3() {
    if (round) {
      const n = round.mails.length;
      if (round.how === "hand") return `<div class="ask-ok" role="status"><h3>Logged ${plural(n, "rate request")}</h3><p class="after">On the case as sent on ${esc(day(round.date))}.</p></div>
        ${rowsHtml(round.mails, false)}<div class="err" id="ask-err"></div>`;
      return `<div class="ask-ok" role="status"><h3>${plural(n, "mail")} ready for Outlook</h3>
          <ol><li>Double-click <b>Make Outlook drafts</b> on your desktop.</li>
            <li>The drafts appear in Outlook, in <b>Drafts › OppList</b>, with your signature.</li>
            <li>Check each one and click Send.</li></ol>
          <p class="after">On the case: ${plural(n, "rate request")} logged today.</p></div>
        ${rowsHtml(round.mails, false)}
        <p class="small m0"><button type="button" class="linkbtn" id="ask-again">Copy them for Outlook again</button> <span class="muted">(if the drafts did not appear)</span></p>
        <div class="err" id="ask-err"></div>
        <details class="ask-alt"><summary>No Make Outlook drafts on this computer? Copy each mail by hand</summary>${rowsHtml(round.mails, true)}
          <div class="ask-logrow"><button type="button" class="linkbtn small" id="ask-pdf">Print or save as PDF</button></div></details>`;
    }
    // the button is in the footer, where Next was, so it is always in view however many vendors there are
    const ms = mails(), n = ms.length;
    return `<div class="ask-make"><p class="m0"><b>Create ${plural(n, "draft")}</b> copies ${n === 1 ? "this mail" : `these ${n} mails`} for Make Outlook drafts. Then double-click <b>Make Outlook drafts</b> on your desktop: one draft per vendor appears in Outlook, in <b>Drafts › OppList</b>, with your signature. Nothing is sent until you click Send there.</p>
        <p class="m0 small muted">These vendors go on the case as asked today. <button type="button" class="linkbtn small" id="ask-what">What is Make Outlook drafts?</button></p></div>
      ${rowsHtml(ms, false)}
      <details class="ask-alt"><summary>No Make Outlook drafts on this computer? Copy each mail by hand</summary>${rowsHtml(ms, true)}
        <div class="ask-logrow"><label class="field">Sent on<input type="date" id="ask-date" value="${todayIso()}" max="${todayIso()}"></label>
          <button type="button" class="btn primary" id="ask-log">Log as sent</button>
          <button type="button" class="linkbtn small" id="ask-pdf">Print or save as PDF</button></div>
        <div class="err" id="ask-logerr" role="alert"></div>
        <p class="hint small">After sending, press Log as sent: that is what puts these rate requests on the case.</p></details>`;
  }

  // ------------------------------------------------------------------ moving between steps
  // what stops the vendors step: { msg, key } (key: a ticked vendor whose address is missing or wrong)
  function vendorsProblem() {
    const n = chosenCount(), map = byKey();
    if (!n) return { msg: "Tick at least one vendor, or type an address." };
    if (n > MAX_DRAFTS) return { msg: `Up to ${MAX_DRAFTS} mails at a time.` };
    for (const k of card.vendors) {
      const v = map.get(k); if (!v || v.to) continue;
      const { good, bad } = parseAddresses(card.to[k]), name = v.name || k;
      if (bad.length) return { msg: `For ${name}, ${bad.join(", ")} does not look like an email address.`, key: k };
      if (!good.length) return { msg: `Type an address for ${name}.`, key: k };
      if (good.length > MAX_PER_MAIL) return { msg: `Up to ${MAX_PER_MAIL} addresses for ${name}.`, key: k };
    }
    return null;
  }
  function mailProblem() {
    if (!card.subject.trim()) return "Write a subject first.";
    if (!card.body.trim()) return "Write the message first.";
    return "";
  }
  function go(k) {
    if (round || k === step || !card) return;
    if (k > 1) {
      const bad = vendorsProblem();
      if (bad) {
        if (step !== 1) { step = 1; paint(); }
        footErr(bad.msg);
        if (bad.key) dr.querySelector(`input[data-to="${CSS.escape(bad.key)}"]`)?.focus();
        return;
      }
    }
    if (k > 2) {
      const bad = mailProblem();
      if (bad) { if (step !== 2) { step = 2; paint(); } footErr(bad); return; }
    }
    step = k; paint();
  }

  // ------------------------------------------------------------------ events
  function wire() {
    dr.addEventListener("click", async ev => {
      const t = ev.target.closest("button"); if (!t || t.disabled) return;
      if (t.dataset.askX !== undefined || t.id === "ask-done") return close();
      if (t.dataset.step) return go(Number(t.dataset.step));
      if (t.dataset.go) return go(Number(t.dataset.go));
      if (t.dataset.all !== undefined) { filter.all = t.dataset.all === "1"; dr.querySelectorAll("[data-all]").forEach(b => b.setAttribute("aria-pressed", String(b === t))); paintList(); return; }
      if (t.dataset.rm !== undefined) { card.adhoc.splice(Number(t.dataset.rm), 1); paintList(); changed(); return; }
      if (t.id === "ask-add") return addAdhoc();
      if (t.dataset.pv) { pv = Math.max(0, Math.min(chosenCount() - 1, pv + Number(t.dataset.pv))); paintVendor(); return; }
      if (t.id === "ask-make") return createDrafts(t);
      if (t.id === "ask-again") return copyAgain(t);
      if (t.id === "ask-what") return helperBox();
      if (t.dataset.copy) return copyPart(t);
      if (t.id === "ask-log") return logAsSent(t);
      if (t.id === "ask-pdf") return printRequest();
    });
    dr.addEventListener("change", ev => {
      const t = ev.target;
      if (t.dataset.v) {
        const k = t.dataset.v;
        if (t.checked && !card.vendors.includes(k)) card.vendors.push(k);
        if (!t.checked) card.vendors = card.vendors.filter(x => x !== k);
        paintList(); changed();
        if (t.checked && !byKey().get(k)?.to) dr.querySelector(`input[data-to="${CSS.escape(k)}"]`)?.focus();
        return;
      }
      if (t.id === "ask-team") { card.teamCc = t.checked; setPrefTeam(t.checked); paintVendor(); changed(); }
    });
    dr.addEventListener("input", ev => {
      const t = ev.target;
      if (t.id === "ask-q") { filter.q = t.value; paintList(); return; }
      if (t.dataset.to) { const v = t.value.trim(); if (v) card.to[t.dataset.to] = v; else delete card.to[t.dataset.to]; changed(); return; }
      if (t.id === "ask-subject") { card.subject = t.value; changed(); return; }
      if (t.id === "ask-body") { card.body = t.value; fit(t); changed(); }
    });
    dr.addEventListener("keydown", ev => { if (ev.target.id === "ask-email" && ev.key === "Enter") { ev.preventDefault(); addAdhoc(); } });
    scrim.addEventListener("click", close);
  }
  // the Add box: several addresses at once, one mail per company; nothing changes until all of it fits
  function addAdhoc() {
    const inp = q("ask-email"), err = q("ask-adderr"), note = q("ask-addnote");
    err.textContent = ""; note.textContent = "";
    const { good, bad } = parseAddresses(inp.value);
    if (bad.length) { err.textContent = `${bad.length === 1 ? "This does" : "These do"} not look like an email address: ${bad.join(", ")}.`; return; }
    if (!good.length) { err.textContent = "Type an address first."; return; }
    const notes = [], tick = [], rest = [], typedNow = card.adhoc.flatMap(x => [x.email, ...(x.cc || [])].map(a => a.toLowerCase()));
    for (const a of good) {
      // an address a listed vendor has: tick that vendor (its name, greeting and CC come with it)
      const listed = vendors().find(x => addressOf(x.to || "").toLowerCase() === a.toLowerCase());
      if (listed) { const was = card.vendors.includes(listed.key) || tick.includes(listed.key); if (!was) tick.push(listed.key);
        notes.push(`${listed.name || listed.key} on the list already has ${a}${was ? ", and it is ticked." : ". It is ticked now."}`); }
      else if (typedNow.includes(a.toLowerCase())) notes.push(`${a} is already on the card.`);
      else rest.push(a);
    }
    // a company already typed in takes the new addresses as CC; the others are new mails
    const adhoc = card.adhoc.map(x => ({ email: x.email, cc: [...(x.cc || [])] })), fresh = [];
    for (const g of byCompany(rest)) {
      const d = domainOf(g.email), same = !isFreeMail(d) && adhoc.find(x => domainOf(x.email) === d);
      if (same) same.cc.push(g.email, ...g.cc); else fresh.push(g);
    }
    if (adhoc.length + fresh.length > MAX_ADHOC) { err.textContent = `Up to ${MAX_ADHOC} typed companies on one card.`; return; }
    if ([...adhoc, ...fresh].some(x => 1 + x.cc.length > MAX_PER_MAIL)) { err.textContent = `Up to ${MAX_PER_MAIL} addresses in one mail.`; return; }
    card.adhoc = [...adhoc, ...fresh];
    card.vendors.push(...tick);
    inp.value = ""; paintList(); changed();
    q("ask-addnote").textContent = notes.join(" ");
    q("ask-email")?.focus();
  }

  // ------------------------------------------------------------------ the drafts and the log
  // everything checked once more right before the drafts are made or the requests logged
  function readyProblem() { return vendorsProblem()?.msg || mailProblem(); }
  async function createDrafts(btn) {
    const err = q("f-err"); err.textContent = "";
    const bad = readyProblem(); if (bad) { err.textContent = bad; return; }
    const ms = mails(), subject = card.subject.trim(), label = btn.textContent;
    if (!reqId) reqId = nowMs().toString(36) + Math.random().toString(36).slice(2, 8);
    const req = draftsRequest(opp, reqId, ms, subject);
    btn.disabled = true; btn.textContent = "Copying…";
    // the clipboard first, while the click still counts as the person's (browsers refuse it later)
    try { await copyText(draftsClip(req)); }
    catch { btn.disabled = false; btn.textContent = label; err.textContent = "Could not copy the mails for Outlook. Use Copy each mail by hand, below."; return; }
    btn.textContent = "Logging on the case…";
    try { await logAsk(todayIso(), ms, "drafts", req); }
    catch (e) {
      btn.disabled = false; btn.textContent = label;
      err.innerHTML = `The mails are copied for Outlook, but they are not logged on the case yet. Press the button again. ${failHtml(e, { part: opp })}`;
    }
  }
  async function logAsSent(btn) {
    const err = q("ask-logerr"); err.textContent = "";
    const bad = readyProblem(); if (bad) { err.textContent = bad; return; }
    const date = q("ask-date").value;
    if (!validIso(date) || date > todayIso()) { err.textContent = "Pick the day the mails went out, up to today."; return; }
    const ms = mails();
    if (!await confirmBox("Log as sent", `Log rate requests to ${plural(ms.length, "vendor")} on ${day(date)}: ${ms.map(m => m.name).join(", ")}? Do this after the mails are sent.`, "Log as sent")) return;
    btn.disabled = true;
    try { await logAsk(date, ms, "hand", null); }
    catch (e) { btn.disabled = false; err.innerHTML = failHtml(e, { part: opp }); }
  }
  // the ask action: OppList takes it in at its next sync (an address typed for a listed vendor with none
  // on file goes with it, the first one, so OppList learns it; the others are CC in the mail only). A typed company
  // is its first address with the others as cc (OppList reads the first). The next round starts with nobody ticked.
  async function logAsk(date, ms, how, req) {
    const map = byKey();
    const to = Object.fromEntries(card.vendors.filter(k => !map.get(k)?.to).map(k => [k, parseAddresses(card.to[k]).good[0]]).filter(([, a]) => a));
    const subject = card.subject.trim();
    await act("ask", opp, { date, vendors: [...card.vendors], adhoc: card.adhoc.map(x => ({ email: x.email, cc: cleanList(x.cc) })), to, subject, teamCc: !!(card.teamCc && teamAddr()) });
    round = { how, date, subject, mails: ms, req };
    card.vendors = []; card.adhoc = [];
    step = 3; paint();
    onLogged?.("logged", ms.length);
    save();
  }
  async function copyAgain(btn) {
    try { await copyText(draftsClip(round.req)); done(btn); }
    catch { q("ask-err").textContent = "Could not copy. Use Copy each mail by hand, below."; }
  }
  async function copyPart(btn) {
    const ms = round ? round.mails : mails(), m = ms[Number(btn.dataset.i)]; if (!m) return;
    const k = btn.dataset.copy, subject = round ? round.subject : card.subject;
    const text = k === "to" ? m.to : k === "cc" ? m.cc.join("; ") : k === "subject" ? subject : m.text;
    try { await copyText(text); done(btn); }
    catch { const e = q("ask-logerr") || q("ask-err"); if (e) e.textContent = "Could not copy. Select the text and press Ctrl+C."; }
  }
  function done(btn) {
    const was = btn.textContent; btn.textContent = "Copied"; btn.classList.add("done");
    setTimeout(() => { btn.textContent = was; btn.classList.remove("done"); }, 1500);
  }
  function printRequest() {
    const subject = round ? round.subject : card.subject, w = window.open("", "_blank");
    if (!w) { const e = q("ask-logerr") || q("ask-err"); if (e) e.textContent = "The browser blocked the print window. Allow pop-ups for this site and try again."; return; }
    w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${esc(subject)}</title><style>
      @page { size: Letter; margin: 0.75in; } body { font-family: Inter, Arial, sans-serif; color: #1A1D21; font-size: 13px; }
      header { display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 2px solid #1F4E8C; padding-bottom: 10px; }
      header b { font-size: 20px; letter-spacing: .04em; color: #1F4E8C; } h1 { font-size: 18px; margin: 18px 0 4px; } .meta { color: #5B6470; margin: 0 0 18px; }
      pre { font: inherit; white-space: pre-wrap; line-height: 1.55; margin: 0; } footer { margin-top: 28px; color: #5B6470; font-size: 11px; }
    </style></head><body><header><b>JoJo Freight</b><span>JoJo Freight Chicago</span></header>
      <h1>Rate request</h1><p class="meta">${esc(subject)} · ${esc(day(todayIso(), true))}</p><pre>${esc(card.body)}</pre>
      <footer>JoJo Freight Chicago · our reference ${esc(opp)}</footer>
      <script>window.onload = () => { window.focus(); window.print(); };<\/script></body></html>`);
    w.document.close();
  }

  // ------------------------------------------------------------------ saving
  function changed() {
    dirty = true; const s = q("ask-saved"); if (s) s.textContent = "Saving…";
    clearTimeout(saveT); saveT = setTimeout(save, 1200);
  }
  async function save() {
    clearTimeout(saveT);
    if (!card) return;
    const data = { vendors: [...card.vendors], adhoc: card.adhoc.map(x => ({ email: x.email, cc: [...(x.cc || [])] })), to: { ...card.to },
      subject: card.subject.slice(0, 300), body: card.body.slice(0, MAX_BODY), teamCc: !!card.teamCc };
    try {
      await saveCard(opp, data);
      dirty = false; saved = { ...data, name: S.me.name || S.me.email, by: S.me.email, at: new Date(nowMs()) };
      const s = q("ask-saved"); if (s) s.innerHTML = savedLine();
    } catch (e) {
      const s = q("ask-saved"); if (s) s.textContent = "Not saved";
      const err = q("ask-err") || q("f-err"); if (err) err.innerHTML = failHtml(e, { part: `${opp} rate request card` });
    }
  }
  function flush() { if (dirty) save(); }

  return {
    toggle() { open ? close() : show(); },
    isOpen: () => open,
    // a sync arrived: the vendor list shows who is asked now (not while an address is typed there)
    refresh() {
      if (!open || !card || !dr) return;
      // a vendor that is no longer on OppList's list (merged into another, say) cannot be asked from here
      if (!round) { const known = byKey(); card.vendors = card.vendors.filter(k => known.has(k)); }
      dr.querySelector(".dr-sub").innerHTML = subLine();
      if (step === 1 && !round && !document.activeElement?.matches?.("#ask-groups input")) paintList();
    },
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
