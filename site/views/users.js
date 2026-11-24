// Users (admin). This page gives a person their place on the site: name, the Handled by name they stand for in
// OppList (Mine and My board use it), role, Monthly report, Costs (the quote card's costs and margins), Active.
// Users are never deleted. On a real installation the account itself (address and password) is made by the
// administrator outside the site; this copy has no accounts, so a person added here simply joins "Viewing as".
import { doc, setDoc, serverTimestamp } from "../store/firestore.js";
import { S, $, main, db, modal, toast, confirmBox, pageHead, go, owners } from "../core.js";
import { esc, stamp, nowMs } from "../lib.js";
import { listUsers } from "../data.js";
import { problemHtml, failHtml } from "../issues.js";
import { ownerOptions } from "./common.js";

const ROLE_L = { admin: "Admin: everything, Users and Issues", member: "Member: the lists, cases, notes" };

export async function viewUsers(p, alive) {
  main.innerHTML = `<div class="page" id="pg-users">${pageHead("Users", "Who can use the site, and the Handled by name each person stands for in OppList.", "", false)}<div id="u-body"><div class="muted loading">Loading…</div></div></div>`;
  let list;
  try { list = await listUsers(); } catch (e) { if (alive()) $("#u-body").innerHTML = `<div class="note-box">${problemHtml(e)}</div>`; return null; }
  if (!alive()) return null;
  const order = { admin: 0, member: 1 };
  list.sort((a, b) => (order[a.role] ?? 3) - (order[b.role] ?? 3) || String(a.name || a.email).localeCompare(String(b.name || b.email)));
  $("#u-body").innerHTML = `<section class="panel"><div class="panel-head"><h2>Users <span class="muted">${list.length}</span></h2><button type="button" class="btn primary" id="u-add">+ Add user</button></div>
    <div class="tablewrap"><table class="grid t-users"><thead><tr><th>Email</th><th>Name</th><th>Handled by</th><th>Role</th><th>Monthly report</th><th>Costs</th><th>Active</th><th>Last seen</th></tr></thead><tbody>
    ${list.map(u => `<tr data-e="${esc(u.email)}" class="click"><td class="nw">${esc(u.email)}</td><td>${esc(u.name || "")}</td><td>${u.owner ? esc(u.owner) : '<span class="muted">not linked</span>'}</td>
      <td>${u.role === "admin" ? "Admin" : "Member"}</td><td>${u.role === "admin" || u.report ? "yes" : '<span class="muted">no</span>'}</td><td>${u.role === "admin" || u.costs ? "yes" : '<span class="muted">no</span>'}</td><td>${u.active === false ? '<span class="err">no</span>' : "yes"}</td>
      <td class="nw">${u.lastLogin ? esc(stamp(u.lastLogin)) : '<span class="muted">never</span>'}</td></tr>`).join("")}
    </tbody></table></div>
    <p class="hint small">Handled by links the person to their name in OppList's Handled by list, for Mine, Take it and My board. Users are never deleted; untick Active to lock someone out. In this demo a person added here appears under Viewing as, and one switched off disappears from it.</p></section>`;
  $("#u-add").onclick = () => userForm(null, list);
  $("#u-body").querySelectorAll("tr[data-e]").forEach(tr => tr.onclick = () => userForm(list.find(u => u.email === tr.dataset.e), list));
  return null;
}

function userForm(u, list) {
  const isNew = !u; u = u || { email: "", name: "", owner: "", role: "member", active: true, report: false, costs: false };
  const self = u.email === S.me.email;
  const bg = modal(isNew ? "Add user" : `Edit ${u.name || u.email}`, `<form id="f-user"><div class="fgrid">
      <label class="field span2">Email<input type="text" name="email" value="${esc(u.email)}"${isNew ? "" : " readonly"} autocomplete="off" spellcheck="false"></label>
      <label class="field span2">Name<input type="text" name="name" value="${esc(u.name || "")}" maxlength="80"></label>
      <label class="field span2">Handled by name in OppList<select name="owner">${ownerOptions(u.owner || "", "Not linked")}</select></label>
      <label class="field span2">Role<select name="role"${self ? " disabled" : ""}>${Object.entries(ROLE_L).map(([v, l]) => `<option value="${v}"${v === u.role ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>${self ? '<span class="hint small">You cannot change your own role.</span>' : ""}</label>
      <label class="chk span2"><input type="checkbox" name="report"${u.role === "admin" || u.report ? " checked" : ""}${self ? " disabled" : ""}> Can open Monthly report</label>
      <label class="chk span2"><input type="checkbox" name="costs"${u.role === "admin" || u.costs ? " checked" : ""}${self ? " disabled" : ""}> Costs: sees and types the costs, markups and margins on quotes</label>
      <label class="chk span2"><input type="checkbox" name="active"${u.active === false ? "" : " checked"}${self ? " disabled" : ""}> Active (untick to lock the account; history stays)</label>
      ${owners().length ? "" : `<p class="hint small full">The Handled by names come from OppList's Settings (owners) with the first sync.</p>`}
    </div><div class="err" id="u-err"></div></form>`,
    { wide: true, buttons: `<button type="button" class="btn" data-close>Cancel</button><button type="submit" form="f-user" class="btn primary">Save user</button>` });
  bg.querySelector("#f-user").onsubmit = async ev => {
    ev.preventDefault(); const f = ev.target, err = bg.querySelector("#u-err"); err.textContent = "";
    const email = f.email.value.trim().toLowerCase(), role = self ? u.role : f.role.value;
    const rec = { email, name: f.name.value.trim(), owner: f.owner.value, role, active: self ? true : f.active.checked,
      report: role === "admin" ? true : f.report.checked, costs: role === "admin" ? true : f.costs.checked, updatedAt: serverTimestamp(), updatedBy: S.me.email, ...(isNew ? { createdAt: serverTimestamp() } : {}) };
    try {
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) throw new Error("Enter a valid email address.");
      if (!rec.name) throw new Error("Enter the person's name.");
      if (isNew && list.some(x => x.email === email)) throw new Error(`${email} is already a user: open it in the list.`);
      const taken = rec.owner && list.find(x => x.email !== email && x.owner === rec.owner && x.active !== false);
      if (taken && !await confirmBox("Same Handled by name", `${rec.owner} is already linked to ${taken.name || taken.email}. Link ${rec.name} to ${rec.owner} as well? Both will see ${rec.owner}'s cases under Mine.`, "Link both")) return;
      await setDoc(doc(db, "users", email), rec, { merge: true });
      bg.close(); toast(`${email} saved.`);
      if (self) Object.assign(S.me, { name: rec.name, owner: rec.owner });
      go("users", { t: nowMs() });
    } catch (e) { err.innerHTML = failHtml(e, { part: "users" }); }
  };
}
