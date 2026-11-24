// Compare rates: opened from the Rate requests
// panel when two vendors or more have rates on a route. The vendors' newest rates on the case side by side for one
// route (a switch on a case with several): line by line, part by part, the lowest of each line in green, a part's sum
// when it has two lines or more, the totals with Lowest (as on the rows), then what each said (valid until, transit,
// carrier, free time, what they did not say, the day they sent them, their note). Each column opens their rates (the
// rates drawer) or, for people who can quote, uses them in the quote card (Rates from: the vendor for each of its
// parts, laid in as when picked by hand). The vendors asked with no rates on the route are listed under it (a reading
// to check, Add rates). Nothing here is saved; it repaints when the case's rates change.
import { S } from "../core.js";
import { esc, day, todayIso } from "../lib.js";
import * as Q from "../quotemail.js";
import * as R from "../ratelib.js";
import { letter } from "../editlib.js";

const WORD = { "AT COST": "At cost", INCLUDED: "Included", "IF APPLICABLE": "If applicable", "N/A": "N/A" };
const ends = (c, rt) => { const e = rt || (c.routes || [])[0]; return e && (e.from || e.to) ? `${Q.placeName(e.from)} to ${Q.placeName(e.to)}` : c.lane || ""; };
// "without FUMIGATION", "without DTHC, FUMIGATION", "without 3 of the lines"
const missText = m => m.length > 2 ? `without ${m.length} of the lines` : `without ${m.map(x => x || "a line").join(", ")}`;

export function comparePanel(opp, cur, { rates = () => ({ best: new Map() }), drafts = () => new Map(), canQuote = () => false, canRates = () => false,
  onRates, onQuote, onDraft } = {}) {
  let open = false, dr = null, scrim = null, route = null, drawn = "";
  const q = id => dr?.querySelector(`#${id}`) || null;

  // ------------------------------------------------------------------ opening and closing
  // the route asked, else the first where two vendors or more have rates, else the case's first
  function show(at) {
    hide();
    const c = cur(); if (!c) return;
    const rs = R.compareRoutes(rates().best);
    route = at !== undefined ? at : rs.length ? rs[0] : R.routesOf(c).length ? R.defaultRoute(c) : null;
    open = true;
    scrim = document.createElement("div"); scrim.className = "ask-scrim";
    dr = document.createElement("aside"); dr.className = "ask-dr cmp-dr";
    dr.setAttribute("role", "dialog"); dr.setAttribute("aria-modal", "false"); dr.setAttribute("aria-labelledby", "cmp-title");
    dr.innerHTML = `<div class="dr-head"><div class="t"><h2 id="cmp-title" tabindex="-1">Compare rates</h2><div class="dr-sub"></div></div>
        <button type="button" class="xbtn" data-cmp-x aria-label="Close">&times;</button></div>
      <div class="dr-body"></div>
      <div class="dr-foot"><span class="info">To change a vendor's rates, open them. Nothing here is saved.</span><span class="grow"></span><button type="button" class="btn" data-cmp-x>Close</button></div>`;
    document.body.append(scrim, dr);
    wire(); paint();
    requestAnimationFrame(() => requestAnimationFrame(() => dr?.classList.add("open")));
    document.addEventListener("keydown", onKey, true);
    setTimeout(() => q("cmp-title")?.focus(), 260);
  }
  function hide() {
    if (!open) return;
    open = false;
    document.removeEventListener("keydown", onKey, true);
    dr?.remove(); scrim?.remove(); dr = scrim = null; drawn = "";
  }
  function close() { hide(); document.querySelector("[data-cmp-open]")?.focus(); }
  function onKey(ev) {
    if (ev.key !== "Escape" || !open || document.querySelector(".modal")) return;
    ev.preventDefault(); ev.stopPropagation(); close();
  }

  // ------------------------------------------------------------------ drawing
  function paint() {
    if (!dr) return;
    const c = cur(); if (!c) { hide(); return; }
    const { best } = rates(), routes = R.routesOf(c), multi = routes.length > 1, rt = R.routeOf(c, route);
    dr.querySelector(".dr-sub").textContent = [opp, rt ? `route ${letter(rt.n)}` : "", R.modeFor(c, route), ends(c, rt), Q.cargoPhrase(c), rt?.incoterm || Q.incotermOf(c)].filter(Boolean).join(" · ");
    const M = R.compareOf(best, R.lowest(best, S.meta?.options === true ? todayIso() : ""), { route, order: [...new Set((c.rr || []).map(x => x.vendor))], today: todayIso() });
    const sw = multi ? `<div><div class="seg cmp-routes" role="group" aria-label="Route">${routes.map(r => `<button type="button" aria-pressed="${r.n === route}" data-cmp-route="${r.n}">${esc(letter(r.n))} · ${esc(ends(c, r))}</button>`).join("")}</div></div>` : "";
    const html = `${sw}<p class="muted small m0">The rates saved on this case for this shipment, side by side. The lowest of each line is in green.</p>
      ${M.cols.length ? tableHtml(c, M, best) : `<div class="empty">No rates saved for ${multi && rt ? `route ${esc(letter(rt.n))}` : "this case"} yet.</div>`}${noneHtml(c, M, multi)}`;
    if (html === drawn) return;                       // the case redrew with nothing new here: the focus stays where it is
    const keep = dr.querySelector(".cmp-wrap")?.scrollLeft || 0;
    dr.querySelector(".dr-body").innerHTML = drawn = html;
    const w = dr.querySelector(".cmp-wrap"); if (w) w.scrollLeft = keep;
  }
  function tableHtml(c, M, best) {
    const n = M.cols.length, opts = S.meta?.options === true;
    const cell = (x, low) => !x ? '<td class="dim">-</td>' : x.none ? '<td class="dim">no price</td>' : x.w ? `<td class="dim">${esc(WORD[x.w] || x.w)}</td>`
      : `<td${low ? ' class="low"' : ""}>${Q.money(x.n)}</td>`;
    let body = "";
    for (const p of M.parts) {
      body += `<tr class="part"><th colspan="${n + 1}" scope="colgroup">${esc(p.seg)}</th></tr>`;
      for (const r of p.rows) body += `<tr><th scope="row">${esc(r.charge || "Not named")}${r.unit ? ` <span>${esc(Q.basisOf("", r.unit))}</span>` : ""}</th>${r.cells.map((x, i) => cell(x, r.low[i])).join("")}</tr>`;
      if (p.sum) body += `<tr class="sub"><th scope="row">${esc(p.seg)}, all of it</th>${p.sum.cells.map((v, i) => v === null ? '<td class="dim">-</td>' : `<td${p.sum.low[i] ? ' class="low"' : ""}>${Q.money(v)}</td>`).join("")}</tr>`;
    }
    body += `<tr class="tot"><th scope="row">Their total, this shipment</th>${M.cols.map(k => `<td>${k.lowest ? '<span class="chip c-green">Lowest</span> ' : ""}${esc(R.totalText(k.doc))}${k.missing.length ? `<span class="miss">${esc(missText(k.missing))}</span>` : ""}</td>`).join("")}</tr>`;
    const notSaid = d => R.SAID.filter(([k]) => d?.said?.[k] === "notsaid").map(([, l]) => l).join(", ");
    const T = [["Valid to", k => k.doc.valid ? `${day(k.doc.valid)}${k.past ? " · past" : ""}` : "", k => k.past ? "past" : ""],
      ["Transit", k => k.doc.transit], ["Carrier", k => k.doc.carrier], ["Free time", k => k.doc.free],
      ["Not said", k => notSaid(k.doc), () => "ns"], ["They sent them on", k => day(k.doc.on)]];
    if (M.cols.some(k => k.doc.note)) T.push(["Their note", k => k.doc.note, () => "note"]);
    let terms = T.map(([label, f, cls = () => ""]) => `<tr><th scope="row">${label}</th>${M.cols.map(k => { const v = f(k) || ""; return `<td class="${v ? cls(k) : "dim"}">${v ? esc(v) : "-"}</td>`; }).join("")}</tr>`).join("");
    // Use in the quote: for people who can quote, a vendor of the case's requests, the rates the quote card takes (its
    // route A's on a case with several routes)
    const asked = new Set((c.rr || []).filter(x => x.vendor && !x.pending).map(x => x.vendor)), quoting = canQuote(c);
    const forQuote = k => quoting && asked.has(k.vendor) && R.ratesForQuote(best, k.vendor, c, opts ? k.label : null)?.id === k.doc.id;
    const lab = k => opts ? ` data-cmp-label="${esc(k.label)}"` : "";
    terms += `<tr class="act"><th scope="row"><span class="sr">Their rates</span></th>${M.cols.map(k => `<td><button type="button" class="linkbtn small" data-cmp-rates="${esc(k.vendor)}"${lab(k)}>Open their rates</button>`
      + `${forQuote(k) ? `<button type="button" class="linkbtn small" data-cmp-quote="${esc(k.vendor)}"${lab(k)}>Use in the quote</button>` : ""}</td>`).join("")}</tr>`;
    // with options, the vendor's name over its options, each option's name under it
    const named = opts && M.cols.some(k => k.label), groups = [];
    for (const k of M.cols) { const g = groups[groups.length - 1]; if (g && g.vendor === k.vendor) g.n++; else groups.push({ vendor: k.vendor, n: 1 }); }
    const head = named
      ? `<tr class="vn"><th scope="col" rowspan="2"><span class="sr">Charge</span></th>${groups.map(g => `<th scope="colgroup"${g.n > 1 ? ` colspan="${g.n}"` : ""}>${esc(g.vendor)}</th>`).join("")}</tr>
        <tr class="on">${M.cols.map(k => `<th scope="col">${esc(k.label || (groups.find(g => g.vendor === k.vendor)?.n > 1 ? "Unnamed" : ""))}</th>`).join("")}</tr>`
      : `<tr class="vn"><th scope="col"><span class="sr">Charge</span></th>${M.cols.map(k => `<th scope="col">${esc(k.vendor)}</th>`).join("")}</tr>`;
    return `<div class="cmp-wrap"><table class="cmp" style="--n:${n}"><colgroup><col class="lab">${"<col>".repeat(n)}</colgroup>
      <thead>${head}</thead>
      <tbody>${body}</tbody><tbody class="terms">${terms}</tbody></table></div>`;
  }
  // the vendors asked (by name) with no rates on this route: a reading to check, their reply, or no reply yet
  function noneHtml(c, M, multi) {
    const have = new Set(M.cols.map(k => k.vendor)), by = new Map(), can = canRates(c), od = drafts();
    for (const x of c.rr || []) {
      if (!x.vendor || x.vendor.includes("@") || have.has(x.vendor)) continue;
      const w = by.get(x.vendor) || { vendor: x.vendor, asked: "", replied: "", quoted: false };
      by.set(x.vendor, { vendor: x.vendor, asked: (x.asked || "") > w.asked ? x.asked : w.asked, replied: (x.replied || "") > w.replied ? x.replied : w.replied, quoted: w.quoted || !!x.quoted });
    }
    if (!by.size) return "";
    const li = [...by.values()].map(v => {
      const d = R.draftsOfVendor(od, v.vendor).find(x => !multi || x.route === route);
      if (d) return `<li><b>${esc(v.vendor)}</b> <span class="muted">· a reading of their mail waits to be checked</span>${can ? ` <button type="button" class="linkbtn small" data-cmp-draft="${esc(d.id)}">Check it</button>` : ""}</li>`;
      if (v.quoted || v.replied) return `<li><b>${esc(v.vendor)}</b> <span class="muted">· ${v.quoted ? "quoted" : "replied"}${v.replied ? ` ${esc(day(v.replied))}` : ""}, their rates not on the case</span>${can ? ` <button type="button" class="linkbtn small" data-cmp-add="${esc(v.vendor)}">Add rates</button>` : ""}</li>`;
      return `<li><b>${esc(v.vendor)}</b> <span class="muted">· asked ${esc(day(v.asked))} · no reply yet</span></li>`;
    }).join("");
    const rt = R.routeOf(c, route);
    return `<div class="cmp-none"><h3>${multi && rt ? `No rates for route ${esc(letter(rt.n))} yet` : "No rates on the case yet"}</h3><ul>${li}</ul></div>`;
  }

  // ------------------------------------------------------------------ events
  function wire() {
    dr.addEventListener("click", ev => {
      if (ev.target.closest("[data-cmp-x]")) { close(); return; }
      const rb = ev.target.closest("[data-cmp-route]");
      if (rb) { route = Number(rb.dataset.cmpRoute); paint(); dr.querySelector(`[data-cmp-route="${route}"]`)?.focus(); return; }
      const at = typeof route === "number" ? route : undefined;
      const ob = ev.target.closest("[data-cmp-rates], [data-cmp-add]");
      if (ob) { hide(); onRates?.(ob.dataset.cmpRates || ob.dataset.cmpAdd, at, ob.dataset.cmpLabel ?? ""); return; }
      const ub = ev.target.closest("[data-cmp-quote]");
      if (ub) { hide(); onQuote?.(ub.dataset.cmpQuote, ub.dataset.cmpLabel ?? null); return; }
      const db = ev.target.closest("[data-cmp-draft]");
      if (db) { hide(); onDraft?.(db.dataset.cmpDraft); }
    });
  }

  return {
    show, leave: hide, isOpen: () => open,
    // the case or its rates changed: drawn again (nothing is typed here); a case no longer there closes it
    refresh() { if (open) paint(); },
  };
}
