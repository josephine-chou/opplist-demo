// Pure helpers: text, dates and the clock. No store and no DOM, so they can be tested in Node.

export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const cut = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
export const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
// what a search box compares: accents, case and extra spaces do not count
export const norm = s => String(s ?? "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/\s+/g, " ").trim();

// ---------------------------------------------------------------- the clock
// The demo data is relative to the day it was generated. Every module asks now() for the time, never the
// computer's clock directly: the page starts at the snapshot's moment and runs on from there, so "3 days
// waiting" and "due today" stay right however long after its making the snapshot is opened.
let shift = 0;
export function setClock(startMillis) { shift = Number.isFinite(startMillis) ? startMillis - Date.now() : 0; }
export const now = () => new Date(Date.now() + shift);
export const nowMs = () => Date.now() + shift;

// ---------------------------------------------------------------- dates (ISO YYYY-MM-DD in the data, the viewer's own day)
const pad = n => String(n).padStart(2, "0");
export const isoOf = (y, m, d) => `${y}-${pad(m)}-${pad(d)}`;
export const todayIso = () => { const d = now(); return isoOf(d.getFullYear(), d.getMonth() + 1, d.getDate()); };
const toDate = iso => new Date(iso + "T00:00:00Z");
// a day that does not exist (2026-13-40) is not a date
export const validIso = iso => { if (!/^\d{4}-\d{2}-\d{2}$/.test(iso || "")) return false; const d = toDate(iso); return !isNaN(d) && d.toISOString().slice(0, 10) === iso; };
// calendar days from a to b (b - a); null when either is not a date
export const daysBetween = (a, b) => validIso(a) && validIso(b) ? Math.round((toDate(b) - toDate(a)) / 86400000) : null;
export const daysSince = iso => daysBetween(iso, todayIso());
export const MON3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
// 'Oct 27'; with the year 'Oct 27, 2026'; '' when not a date
export function day(iso, year = false) {
  if (!validIso(String(iso || "").slice(0, 10))) return "";
  const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  return `${MON3[m - 1]} ${d}${year ? `, ${y}` : ""}`;
}
// '2026-10' -> 'October 2026'
export function monthName(ym) { const [y, m] = String(ym || "").split("-").map(Number); return y && m ? `${MONTHS[m - 1]} ${y}` : ""; }
// a store time, a Date or an ISO string as a Date (null when none)
export function asDate(v) {
  if (!v) return null;
  if (v.toDate) return v.toDate();
  if (v instanceof Date) return v;
  const d = new Date(v); return isNaN(d) ? null : d;
}
export function stamp(v) {
  const d = asDate(v); if (!d) return "";
  const sameYear = d.getFullYear() === now().getFullYear();
  return d.toLocaleString("en-US", { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }), hour: "numeric", minute: "2-digit" });
}
