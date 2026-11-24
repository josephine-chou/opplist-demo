// Issues (admin): the problem reports people sent and the failures the app caught, newest first. The same failure
// caught for several people folds into one line with a count; a report someone sent stands on its own. Open one for
// the details, email the person, keep a note and mark it resolved. Records are never deleted.
import { doc, getDoc, getDocs, updateDoc, collection, query, orderBy, limit, writeBatch, serverTimestamp } from "../store/firestore.js";
import { db, S, $, main, go, modal, toast, APP_VERSION, pageHead } from "../core.js";
import { stamp as fmtTs, nowMs } from "../lib.js";
import { esc } from "../lib.js";
import { filters, grp, select, statusLine } from "./common.js";
import { KIND, issueCount, problemHtml, normRef, cut, copyText } from "../issues.js";

const STATUS = [["open", "Open"], ["resolved", "Resolved"], ["all", "All"]];
const TYPES = [["all", "All"], ["sent", "Sent by people"], ["error", "Errors caught"]];
const typeOk = (r, t) => t === "all" || (t === "sent" ? r.reported : r.kind === t);
const LIMIT = 500;

export async function viewIssues(p, alive) {
  const st = STATUS.some(x => x[0] === p.st) ? p.st : "open";
  const ty = TYPES.some(x => x[0] === p.t) ? p.t : "all";
  main.innerHTML = `<div class="page">` + pageHead("Issues", "Problem reports people sent and failures the site caught. Records are never deleted.", "", false)
    + filters(grp("Status", select("sel-st", STATUS, st, { minWidth: 140 })) + grp("Type", select("sel-t", TYPES, ty, { minWidth: 210 }))
      + grp("Open a reference", `<form class="refq" id="f-ref"><input id="q-ref" placeholder="E-7K3Q9" maxlength="9" autocomplete="off" spellcheck="false"><button class="btn" type="submit">Open</button></form>`))
    + `<div id="status"></div><div id="iss" class="muted">Loading&hellip;</div></div>`;
  $("#sel-st").onchange = ev => go("issues", { st: ev.target.value, t: ty });
  $("#sel-t").onchange = ev => go("issues", { st, t: ev.target.value });
  let recs;
  try { recs = (await getDocs(query(collection(db, "issues"), orderBy("at", "desc"), limit(LIMIT)))).docs.map(d => d.data()); }
  catch (e) { if (alive()) $("#iss").innerHTML = `<div class="errline">Could not load the issues: ${problemHtml(e)}</div>`; return; }
  if (!alive()) return;
  const changed = () => { draw(); issueCount(true); };
  $("#f-ref").onsubmit = ev => {
    ev.preventDefault();
    const ref = normRef($("#q-ref").value);
    if (!ref) { toast("A reference looks like E-7K3Q9 or R-7K3Q9."); return; }
    openIssue(ref, recs, changed);
  };
  function draw() {
    const shown = recs.filter(r => (st === "all" || r.status === st) && typeOk(r, ty));
    const groups = fold(shown);
    const open = recs.filter(r => r.status === "open");
    $("#status").innerHTML = statusLine([`${open.length} open`, `${open.filter(r => r.reported).length} of them sent by people`,
      recs.length >= LIMIT ? `The newest ${LIMIT} records` : `${recs.length} record${recs.length === 1 ? "" : "s"} in all`, "The same error for several people is one line, with the count", `This site: version ${APP_VERSION}`]);
    const el = $("#iss"); el.className = "";
    if (!groups.length) { el.innerHTML = `<div class="panel"><p class="empty m0">${st === "open" ? "No open issues." : "Nothing here."}</p></div>`; return; }
    el.innerHTML = `<div class="panel"><div class="tablewrap"><table class="grid ist"><thead><tr><th>Ref</th><th>When</th><th>Who</th><th>Where</th><th>What</th><th>Type</th><th class="r">Times</th><th>Status</th></tr></thead><tbody>${groups.map(row).join("")}</tbody></table></div></div>`;
    el.querySelectorAll("tr[data-ref]").forEach(tr => tr.onclick = ev => { ev.preventDefault(); openIssue(tr.dataset.ref, recs, changed); });
  }
  draw();
  if (p.ref) { const ref = normRef(p.ref); if (ref) openIssue(ref, recs, changed); }
}

// a report someone sent stands alone; failures the app caught with the same signature (and status) fold together
function fold(recs) {
  const m = new Map();
  for (const r of recs) { const k = r.reported || !r.sig ? r.ref : `${r.status}|${r.sig}`; if (!m.has(k)) m.set(k, []); m.get(k).push(r); }
  return [...m.values()];
}
const who = r => esc(r.name || r.by);
const pills = r => `<span class="pill t-${r.kind}">${esc(KIND[r.kind] || r.kind)}</span>${r.reported && r.kind !== "report" ? ' <span class="pill sent">Sent</span>' : ""}`;
const statusPill = r => r.status === "resolved" ? '<span class="pill done">Resolved</span>' : '<span class="pill prog">Open</span>';
// a folded line names up to three people and the pages involved
function groupWho(g) {
  const names = [...new Set(g.map(x => x.name || x.by))];
  return names.length > 3 ? `${names.length} people` : names.map(esc).join(", ");
}
function groupWhere(g) {
  const ws = [...new Set(g.map(x => x.where || ""))];
  return ws.length === 1 ? ws[0] : ws[0].split(", ")[0];
}
function row(g) {
  const r = g[0];
  const what = r.kind === "report" ? esc(cut(r.note || r.title, 160)) : `${esc(cut(r.title, 160))}${r.reported && r.note ? `<div class="said">&ldquo;${esc(cut(r.note, 140))}&rdquo;</div>` : ""}`;
  return `<tr data-ref="${r.ref}" class="${r.status === "resolved" ? "res" : ""}"><td class="nw"><a href="#" class="ref">${r.ref}</a></td><td class="nw">${fmtTs(r.at)}</td>
    <td>${g.length > 1 ? groupWho(g) : who(r)}</td><td>${esc(g.length > 1 ? groupWhere(g) : r.where || "")}</td><td class="what">${what}</td>
    <td class="nw">${pills(r)}</td><td class="r">${g.length > 1 ? g.length : ""}</td><td>${statusPill(r)}</td></tr>`;
}

function device(c = {}) {
  const ua = c.ua || "";
  const br = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "Browser";
  const ver = (ua.match(/(?:Edg|OPR|Chrome|Firefox|Version)\/(\d+)/) || [])[1];
  const os = /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Linux/.test(ua) ? "Linux" : "";
  return [`${br}${ver ? ` ${ver}` : ""}${os ? ` on ${os}` : ""}`, c.screen && `screen ${c.screen}`, c.window && `window ${c.window}`,
    c.app && `version ${c.app}${c.app !== APP_VERSION ? ` (the site is now ${APP_VERSION})` : ""}`, c.online === false && "offline"].filter(Boolean).join(", ");
}
// the page the person was on, as the administrator opens it
function pageLink(r) {
  const h = r.hash || ""; return h.startsWith("#/") ? h : "";
}

// the whole record as plain text, for Copy details (to paste into an email or a chat with whoever fixes the site)
function issueText(r, same, note) {
  const link = pageLink(r);
  const lines = [
    `OppList issue ${r.ref} (${KIND[r.kind] || r.kind}${r.reported && r.kind !== "report" ? ", sent by email" : ""})`,
    `When: ${fmtTs(r.at)}${r.reported && r.reportedAt && r.kind !== "report" ? ` (sent ${fmtTs(r.reportedAt)})` : ""}`,
    `Who: ${r.name ? `${r.name} <${r.by}>` : r.by}${r.role ? `, ${r.role}` : ""}`,
    `Where: ${r.where || ""}`,
    ...(link ? [`Page: ${location.origin}${location.pathname}${link}`] : []),
    ...(r.note ? [`${r.kind === "report" ? "Report" : "They wrote"}: ${r.note}`] : []),
    ...(r.kind !== "report" ? [`Problem: ${r.title || ""}`] : []),
    ...(r.kind !== "report" && r.message && r.message !== r.title ? [`Error: ${r.message}${r.code ? ` (${r.code})` : ""}`] : r.code ? [`Code: ${r.code}`] : []),
    ...(r.file ? [`File: ${r.file}`] : []),
    `Device: ${device(r.client)}`,
    `Status: ${r.status === "resolved" ? `Resolved ${fmtTs(r.resolvedAt)}${r.resolvedBy ? ` by ${r.resolvedBy}` : ""}` : "Open"}`,
    ...(same.length ? [`Same error: ${same.length} other record${same.length === 1 ? "" : "s"}: ${same.slice(0, 30).map(x => `${x.ref} (${fmtTs(x.at)}, ${x.name || x.by}${x.status === "resolved" ? ", resolved" : ""})`).join("; ")}${same.length > 30 ? "; …" : ""}`] : []),
    ...(note ? [`Note: ${note}`] : []),
  ];
  if (r.stack) lines.push("", "Error trace:", r.stack);
  return lines.join("\r\n");
}

async function openIssue(ref, recs, onChange) {
  let r = recs.find(x => x.ref === ref);
  if (!r) {
    try { const d = await getDoc(doc(db, "issues", ref)); if (d.exists()) { r = d.data(); recs.push(r); } }
    catch (e) { modal(`Reference ${ref}`, `<p>${problemHtml(e)}</p>`); return; }
  }
  if (!r) { modal(`Reference ${ref}`, `<p>No record with this reference. Check the letters, or it may still be on its way (the person was offline).</p>`); return; }
  const same = r.sig && r.kind !== "report" ? recs.filter(x => x.sig === r.sig && x.ref !== r.ref) : [];
  const openSame = r.status === "open" ? same.filter(x => x.status === "open") : [];
  const reply = `mailto:${r.by}?subject=${encodeURIComponent(`Re: [OppList] ${r.ref}: ${cut(r.title || "Problem report", 80)}`)}&body=${encodeURIComponent(`Hi ${(r.name || "").split(" ")[0] || "there"},\r\n\r\n`)}`;
  const link = pageLink(r);
  const rows = [
    ["When", `${fmtTs(r.at)}${r.reported && r.reportedAt && r.kind !== "report" ? ` <span class="muted">(sent ${fmtTs(r.reportedAt)})</span>` : ""}`],
    ["Who", `${esc(r.name || "")} <span class="muted">${esc(r.by)}${r.role ? `, ${esc(r.role)}` : ""}</span>`],
    ["Where", `${esc(r.where || "")}${link ? ` &middot; <a href="${esc(link)}" class="pg">Open this page</a>` : ""}`],
    ...(r.note ? [[r.kind === "report" ? "Report" : "They wrote", `<div class="said-full">${esc(r.note)}</div>`]] : []),
    ...(r.kind !== "report" ? [["Problem", esc(r.title || "")]] : []),
    ...(r.kind !== "report" && r.message && r.message !== r.title ? [["Error", `${esc(r.message)}${r.code ? ` <span class="muted">(${esc(r.code)})</span>` : ""}`]] : r.code ? [["Code", esc(r.code)]] : []),
    ...(r.file ? [["File", esc(r.file)]] : []),
    ["Device", esc(device(r.client))],
    ["Status", r.status === "resolved" ? `Resolved ${fmtTs(r.resolvedAt)}${r.resolvedBy ? ` by ${esc(r.resolvedBy)}` : ""}` : "Open"],
    ...(same.length ? [["Same error", `${same.length} other record${same.length === 1 ? "" : "s"}: ${same.slice(0, 12).map(x => `<a href="#" class="ref" data-other="${x.ref}">${x.ref}</a> <span class="muted">${fmtTs(x.at)}, ${esc(x.name || x.by)}${x.status === "resolved" ? ", resolved" : ""}</span>`).join("; ")}${same.length > 12 ? "; …" : ""}`]] : []),
    ...(r.stack ? [["Technical", `<details><summary>Error trace (for whoever fixes the site)</summary><pre>${esc(r.stack)}</pre></details>`]] : []),
  ];
  const bg = modal(`${r.ref} · ${KIND[r.kind] || r.kind}${r.reported && r.kind !== "report" ? ", sent by email" : ""}`, `<div class="iss-d">
      <table>${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</table>
      <div class="field"><label for="adm-note">Your note (only you see it)</label><textarea id="adm-note" rows="3" maxlength="4000">${esc(r.adminNote || "")}</textarea></div>
      ${openSame.length ? `<div class="muted" style="font-size:12.5px">Marking it resolved also resolves the ${openSame.length} other open record${openSame.length === 1 ? "" : "s"} of the same error.</div>` : ""}
      <div class="err" id="adm-err"></div></div>`,
    { wide: true, buttons: `<button type="button" class="btn" data-close>Close</button><button type="button" class="btn" id="adm-copy" title="Everything in this record, error trace included, as text to paste">Copy details</button><a class="btn" href="${esc(reply)}">Email ${esc((r.name || r.by).split(/[ @]/)[0])}</a><button type="button" class="btn" id="adm-save">Save note</button><button type="button" class="btn primary" id="adm-st">${r.status === "open" ? "Mark resolved" : "Reopen"}</button>` });

  bg.querySelector("a.pg")?.addEventListener("click", () => bg.close());
  bg.querySelectorAll("[data-other]").forEach(a => a.onclick = ev => { ev.preventDefault(); bg.close(); openIssue(a.dataset.other, recs, onChange); });
  const note = () => bg.querySelector("#adm-note").value.trim(), errEl = bg.querySelector("#adm-err");
  bg.querySelector("#adm-copy").onclick = ev => { copyText(issueText(r, same, note()), ev.currentTarget); toast(`${r.ref}: details copied.`); };
  bg.querySelector("#adm-save").onclick = async ev => {
    ev.currentTarget.disabled = true;
    try { await updateDoc(doc(db, "issues", r.ref), { adminNote: note() }); r.adminNote = note(); toast(`${r.ref}: note saved.`); bg.close(); onChange(); }
    catch (e) { errEl.innerHTML = problemHtml(e); ev.currentTarget.disabled = false; }
  };
  bg.querySelector("#adm-st").onclick = async ev => {
    const status = r.status === "open" ? "resolved" : "open", targets = [r, ...(status === "resolved" ? openSame : [])];
    ev.currentTarget.disabled = true;
    try {
      const b = writeBatch(db), n = note();
      for (const x of targets) b.update(doc(db, "issues", x.ref), { status, adminNote: x === r ? n : x.adminNote || n, resolvedAt: status === "resolved" ? serverTimestamp() : null, resolvedBy: status === "resolved" ? S.me.email : null });
      await b.commit();
      const now = new Date(nowMs());
      for (const x of targets) Object.assign(x, { status, adminNote: x === r ? n : x.adminNote || n, resolvedAt: status === "resolved" ? now : null, resolvedBy: status === "resolved" ? S.me.email : null });
      toast(status === "resolved" ? `${r.ref} resolved${targets.length > 1 ? ` with ${targets.length - 1} other${targets.length > 2 ? "s" : ""}` : ""}.` : `${r.ref} reopened.`);
      bg.close(); onChange();
    } catch (e) { errEl.innerHTML = problemHtml(e); ev.currentTarget.disabled = false; }
  };
}
