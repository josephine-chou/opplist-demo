// The document store of this copy of the site. The page modules were written against a cloud document
// database; here the same calls (doc, collection, query, onSnapshot, getDoc, setDoc, writeBatch ...) are
// answered from memory. The documents come from one snapshot file that `python -m opplist demo` writes
// through OppList's own push code. Nothing in this module opens a connection.
//
// What a visitor does is written into the same memory, so every listener sees it at once, and is kept in
// the browser's localStorage so that a reload keeps it. resetStore() forgets it.
//
// No DOM in here: the module also runs in Node for its test (tests/store_test.mjs).
import { now } from "../lib.js";

// ---------------------------------------------------------------- times
export class Timestamp {
  constructor(ms) { this.ms = Number(ms) || 0; }
  static fromMillis(ms) { return new Timestamp(ms); }
  static fromDate(d) { return new Timestamp(d.getTime()); }
  static now() { return new Timestamp(now().getTime()); }
  toMillis() { return this.ms; }
  toDate() { return new Date(this.ms); }
  get seconds() { return Math.floor(this.ms / 1000); }
  isEqual(other) { return other instanceof Timestamp && other.ms === this.ms; }
}
const SERVER_TIME = Object.freeze({ __serverTime: true });
export const serverTimestamp = () => SERVER_TIME;

// ---------------------------------------------------------------- errors
// the codes are the ones the pages already know how to explain (core.js, friendlyError)
function fail(code, message) { const e = new Error(message); e.code = code; return e; }
const denied = what => fail("permission-denied", `Not allowed for the person you are viewing as: ${what}.`);
const unsupported = what => fail("unimplemented", `The demo store does not answer this: ${what}.`);

// ---------------------------------------------------------------- the documents
const db = { name: "demo store" };
const docs = new Map();          // "cases/OPP-1201" -> the document's fields
let changedPaths = new Map();    // what the visitor wrote: path -> fields, or null for a deleted document
let storage = null, storageKey = "", who = () => null, standIn = null;
let seeded = false, seedWaiters = [];
const DROPPED = new Set(["uploads"]);   // counted and not kept: the parts of an uploaded file
export const stats = { uploads: 0 };

export function getFirestore() { return db; }

// a value as the store keeps it: plain data, times as Timestamp. Every read hands out a copy.
function copy(v) {
  if (v instanceof Timestamp) return new Timestamp(v.ms);
  if (v instanceof Date) return new Timestamp(v.getTime());
  if (Array.isArray(v)) return v.map(copy);
  if (v && typeof v === "object") { const o = {}; for (const k of Object.keys(v)) if (v[k] !== undefined) o[k] = copy(v[k]); return o; }
  return v;
}
// snapshot and localStorage form: a time is { "__time": "2026-11-24T09:25:00" }, the wall clock of the demo
// day, read in the visitor's own time zone so that "today" is the same day for everyone
const pad = n => String(n).padStart(2, "0");
const wall = ms => { const d = new Date(ms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, "0")}`; };
export function wallToMillis(text) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?/.exec(String(text || ""));
  if (!m) return 0;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], +(m[7] || "0").padEnd(3, "0")).getTime();
}
function decode(v) {
  if (Array.isArray(v)) return v.map(decode);
  if (v && typeof v === "object") {
    if (typeof v.__time === "string" && Object.keys(v).length === 1) return new Timestamp(wallToMillis(v.__time));
    const o = {}; for (const k of Object.keys(v)) o[k] = decode(v[k]); return o;
  }
  return v;
}
function encode(v) {
  if (v instanceof Timestamp) return { __time: wall(v.ms) };
  if (Array.isArray(v)) return v.map(encode);
  if (v && typeof v === "object") { const o = {}; for (const k of Object.keys(v)) o[k] = encode(v[k]); return o; }
  return v;
}

// Loads the snapshot ({ made, now, docs }) and lays the visitor's earlier changes over it.
//   storage: a localStorage-like object, or null (a private window, Node)
//   viewer:  () => the users/ document of the person the site is viewed as (the rules below ask it)
//   helper:  an object with onWrite(path, fields): what plays OppList's part for a few actions (standin.js)
export function seed(snapshot, { storage: st = null, viewer = () => null, helper = null } = {}) {
  docs.clear(); changedPaths = new Map(); listeners.clear();
  for (const [path, fields] of Object.entries(snapshot.docs || {})) docs.set(path, decode(fields));
  storage = st; who = viewer; standIn = helper;
  storageKey = `opplist-demo.site.changes:${snapshot.made || ""}`;
  if (storage) {
    try {
      // changes kept for an older snapshot do not fit this one: forget them
      for (let i = storage.length - 1; i >= 0; i--) { const k = storage.key(i); if (k && k.startsWith("opplist-demo.site.changes:") && k !== storageKey) storage.removeItem(k); }
      const kept = JSON.parse(storage.getItem(storageKey) || "{}");
      for (const [path, fields] of Object.entries(kept)) {
        if (fields === null) { docs.delete(path); changedPaths.set(path, null); }
        else { const d = decode(fields); docs.set(path, d); changedPaths.set(path, d); }
      }
    } catch (_) { /* nothing kept, or it cannot be read: the snapshot alone */ }
  }
  seeded = true;
  for (const fn of seedWaiters.splice(0)) fn();
}
const whenSeeded = () => seeded ? Promise.resolve() : new Promise(res => seedWaiters.push(res));
function keep() {
  if (!storage) return;
  try { const out = {}; for (const [p, d] of changedPaths) out[p] = d === null ? null : encode(d); storage.setItem(storageKey, JSON.stringify(out)); }
  catch (_) { /* full or switched off: the changes live until the page is closed */ }
}
// how many documents the visitor has written or changed (the menu shows Reset demo when there are any)
export const changeCount = () => changedPaths.size;
// forgets everything the visitor did; the caller reloads the page
export function resetStore() {
  if (storage) { try { storage.removeItem(storageKey); } catch (_) { /* nothing to forget */ } }
  changedPaths = new Map();
}

// ---------------------------------------------------------------- references and queries
let idSeq = 0;
const ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
function newId() {
  let s = "";
  const t = (now().getTime() * 1000 + (idSeq++ % 1000)).toString(36);
  for (let i = 0; i < 20 - t.length; i++) s += ID_CHARS[Math.floor(Math.random() * ID_CHARS.length)];
  return (s + t).slice(0, 20);
}
export function collection(parent, ...parts) {
  const path = parts.join("/");
  if (!path || path.split("/").length % 2 === 0) throw unsupported(`collection "${path}"`);
  if (path.includes("/")) throw unsupported("a collection inside a document");
  return { kind: "collection", path, id: path };
}
export function doc(parent, ...parts) {
  if (parent && parent.kind === "collection") {
    const id = parts.length ? String(parts[0]) : newId();
    return { kind: "doc", path: `${parent.path}/${id}`, id, parent };
  }
  const path = parts.join("/"), bits = path.split("/");
  if (bits.length !== 2 || !bits[0] || !bits[1]) throw unsupported(`document "${path}"`);
  return { kind: "doc", path, id: bits[1], parent: { kind: "collection", path: bits[0], id: bits[0] } };
}
const OPS = { "==": (a, b) => cmp(a, b) === 0, ">": (a, b) => comparable(a, b) && cmp(a, b) > 0, ">=": (a, b) => comparable(a, b) && cmp(a, b) >= 0, "<": (a, b) => comparable(a, b) && cmp(a, b) < 0, "<=": (a, b) => comparable(a, b) && cmp(a, b) <= 0 };
export function where(field, op, value) { if (!OPS[op]) throw unsupported(`where ${op}`); return { kind: "where", field, op, value }; }
export function orderBy(field, dir = "asc") { return { kind: "orderBy", field, dir }; }
export function limit(n) { return { kind: "limit", n }; }
export function query(col, ...parts) {
  if (!col || col.kind !== "collection") throw unsupported("a query over anything but one collection");
  return { kind: "query", col, wheres: parts.filter(p => p.kind === "where"), orders: parts.filter(p => p.kind === "orderBy"), limit: parts.find(p => p.kind === "limit")?.n ?? null };
}
const plain = v => v instanceof Timestamp ? v.ms : v;
const comparable = (a, b) => a !== undefined && a !== null && b !== undefined && b !== null && typeof plain(a) === typeof plain(b) && (a instanceof Timestamp) === (b instanceof Timestamp);
function cmp(a, b) { a = plain(a); b = plain(b); if (a === b) return 0; if (a === undefined || a === null) return -1; if (b === undefined || b === null) return 1; return a < b ? -1 : a > b ? 1 : 0; }
const field = (fields, name) => name.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), fields);

function run(q) {
  if (q.kind === "collection") q = query(q);
  const prefix = q.col.path + "/";
  let rows = [];
  for (const [path, fields] of docs) {
    if (!path.startsWith(prefix)) continue;
    if (q.wheres.every(w => OPS[w.op](field(fields, w.field), w.value))) rows.push([path, fields]);
  }
  if (q.orders.length) {
    // a document without the field is not in an ordered answer
    rows = rows.filter(([, f]) => q.orders.every(o => field(f, o.field) !== undefined));
    rows.sort((x, y) => { for (const o of q.orders) { const c = cmp(field(x[1], o.field), field(y[1], o.field)); if (c) return o.dir === "desc" ? -c : c; } return x[0] < y[0] ? -1 : 1; });
  } else rows.sort((x, y) => (x[0] < y[0] ? -1 : 1));
  if (q.limit !== null) rows = rows.slice(0, q.limit);
  return rows;
}

// ---------------------------------------------------------------- who may read and write what
// The few rules a visitor can meet, written for this copy. `me` is the users/ document of the person the
// site is viewed as. pushed: what OppList writes; the site only reads it.
const PUSHED = new Set(["meta", "lists", "cases", "reports", "rateDrafts", "actionResults"]);
const isAdmin = me => me?.role === "admin";
function mayRead(col, id, fields) {
  const me = who();
  if (!me || me.active === false) return col === "users" && !!me && id === me.email;
  if (col === "quoteCosts") return isAdmin(me) || me.costs === true;
  if (col === "evidence" || col === "issues" || col === "uploads") return isAdmin(me);
  if (col === "reports") return isAdmin(me) || me.report === true;
  if (col === "users") return isAdmin(me) || id === me.email || id === null;
  return true;
}
function mayWrite(col, id, old, next, how) {
  const me = who();
  if (!me || me.active === false) return false;
  if (PUSHED.has(col)) return false;
  if (col === "users") {
    if (isAdmin(me)) return true;
    // a person's own sign-in time, nothing else
    return how === "update" && id === me.email && Object.keys(next).every(k => k === "lastLogin");
  }
  if (col === "actions" || col === "evidence" || col === "quotes" || col === "quoteCosts" || col === "vendorRates" || col === "uploads") {
    return !old && next.by === me.email;   // written once, in the writer's own name
  }
  if (col === "notes") return old ? old.by === me.email : next.by === me.email;
  if (col === "cards") return next.by === me.email;
  if (col === "issues") return old ? isAdmin(me) || old.by === me.email : true;
  return false;
}

// ---------------------------------------------------------------- snapshots as the pages read them
function docSnap(ref) {
  const fields = docs.get(ref.path);
  return { id: ref.id, ref, exists: () => fields !== undefined, data: () => (fields === undefined ? undefined : copy(fields)) };
}
function querySnap(q) {
  const list = run(q).map(([path, fields]) => { const id = path.slice(path.indexOf("/") + 1); return { id, ref: { kind: "doc", path, id }, exists: () => true, data: () => copy(fields) }; });
  return { docs: list, size: list.length, empty: !list.length, forEach: fn => list.forEach(fn) };
}
const colOf = target => target.kind === "doc" ? target.path.split("/")[0] : (target.col || target).path;
function checkRead(target) {
  const col = colOf(target);
  if (target.kind === "doc") { if (!mayRead(col, target.id, docs.get(target.path))) throw denied(`reading ${col}`); return; }
  // a list of a collection: the rule must hold for every document a query could return
  if (!mayRead(col, null, null)) throw denied(`reading ${col}`);
  if (col === "users" && !isAdmin(who())) throw denied("the list of users");
}

export async function getDoc(ref) { await whenSeeded(); checkRead(ref); return docSnap(ref); }
export async function getDocs(q) { await whenSeeded(); checkRead(q); return querySnap(q); }
export async function getCountFromServer(q) { await whenSeeded(); checkRead(q); const n = run(q).length; return { data: () => ({ count: n }) }; }

// ---------------------------------------------------------------- listeners
const listeners = new Set();
const sameTarget = (l, col) => colOf(l.target) === col;
function tell(l) {
  if (l.off) return;
  let snap;
  try { checkRead(l.target); snap = l.target.kind === "doc" ? docSnap(l.target) : querySnap(l.target); }
  catch (e) { listeners.delete(l); l.off = true; if (l.error) l.error(e); return; }
  // the same answer as last time is not announced again
  const sig = JSON.stringify(l.target.kind === "doc" ? encode(snap.data() ?? null) : snap.docs.map(d => [d.id, encode(d.data())]));
  if (sig === l.sig) return;
  l.sig = sig;
  try { l.next(snap); } catch (e) { console.error(e); }
}
export function onSnapshot(target, next, error) {
  const l = { target, next, error, sig: undefined, off: false };
  listeners.add(l);
  whenSeeded().then(() => queueMicrotask(() => tell(l)));
  return () => { l.off = true; listeners.delete(l); };
}
function announce(cols) {
  // after the write has returned to its caller, as a real store answers a little later
  queueMicrotask(() => { for (const l of [...listeners]) if ([...cols].some(c => sameTarget(l, c))) tell(l); });
}
// the person the site is viewed as changed: every listener is asked again (a rule may now say no, or yes)
export function viewerChanged() { for (const l of [...listeners]) { l.sig = undefined; tell(l); } }

// ---------------------------------------------------------------- writes
function stampTimes(v, at) {
  if (v === SERVER_TIME) return new Timestamp(at);
  if (v instanceof Timestamp || v instanceof Date) return copy(v);
  if (Array.isArray(v)) return v.map(x => stampTimes(x, at));
  if (v && typeof v === "object") { const o = {}; for (const k of Object.keys(v)) if (v[k] !== undefined) o[k] = stampTimes(v[k], at); return o; }
  return v;
}
function setPath(target, name, value) {
  const keys = name.split("."); let o = target;
  for (const k of keys.slice(0, -1)) { o[k] = o[k] && typeof o[k] === "object" && !Array.isArray(o[k]) ? { ...o[k] } : {}; o = o[k]; }
  o[keys[keys.length - 1]] = value;
}
// each write: { how: "set" | "merge" | "update" | "delete", ref, data }. All of them are checked before any
// is applied, as a batch in the real store either lands whole or not at all.
function apply(writes) {
  const at = now().getTime(), todo = [];
  for (const w of writes) {
    const col = colOf(w.ref), old = docs.get(w.ref.path);
    if (w.how === "delete") { if (!mayWrite(col, w.ref.id, old, {}, "delete")) throw denied(`deleting ${col}`); todo.push([w.ref, col, null]); continue; }
    const data = stampTimes(w.data || {}, at);
    if (w.how === "update" && old === undefined) throw fail("not-found", `No document to update: ${w.ref.path}`);
    if (!mayWrite(col, w.ref.id, old, data, w.how === "set" ? "set" : "update")) throw denied(`writing ${col}`);
    let next;
    if (w.how === "set") next = data;
    else { next = copy(old || {}); for (const k of Object.keys(data)) setPath(next, k, data[k]); }
    // a person's "last seen" time alone is not something the visitor did: not kept, not counted
    const quiet = col === "users" && w.how === "update" && Object.keys(data).every(k => k === "lastLogin") && !changedPaths.has(w.ref.path);
    todo.push([w.ref, col, next, quiet]);
  }
  const cols = new Set();
  for (const [ref, col, next, quiet] of todo) {
    cols.add(col);
    if (DROPPED.has(col)) { stats.uploads++; continue; }
    if (next === null) { docs.delete(ref.path); changedPaths.set(ref.path, null); }
    else { docs.set(ref.path, next); if (!quiet) changedPaths.set(ref.path, next); }
  }
  keep();
  announce(cols);
  if (standIn) for (const [ref, col, next] of todo) if (next) { try { standIn.onWrite(col, ref.id, copy(next)); } catch (e) { console.error(e); } }
}
export async function setDoc(ref, data, options = {}) { await whenSeeded(); apply([{ how: options.merge ? "merge" : "set", ref, data }]); }
export async function updateDoc(ref, data) { await whenSeeded(); apply([{ how: "update", ref, data }]); }
export function writeBatch() {
  const writes = [];
  const batch = {
    set(ref, data, options = {}) { writes.push({ how: options.merge ? "merge" : "set", ref, data }); return batch; },
    update(ref, data) { writes.push({ how: "update", ref, data }); return batch; },
    delete(ref) { writes.push({ how: "delete", ref }); return batch; },
    async commit() { await whenSeeded(); apply(writes.splice(0)); },
  };
  return batch;
}
// What plays OppList's part writes through here: the documents OppList would push (an answer to an action,
// a reading), which the rules above let nobody on the site write.
// kept: false for what need not survive a reload (the time of the last update)
export function pushAsOppList(path, fields, { kept = true } = {}) {
  const [col, id] = path.split("/");
  const next = stampTimes(fields, now().getTime());
  docs.set(path, next);
  if (kept) { changedPaths.set(path, next); keep(); }
  announce(new Set([col]));
  return id;
}
// one document as it is, for the stand-in and the tests (no rule asked)
export const peek = path => { const d = docs.get(path); return d === undefined ? undefined : copy(d); };
// every document of one collection as it is (no rule asked)
export const list = col => { const out = []; for (const [p, d] of docs) if (p.startsWith(col + "/")) out.push(copy(d)); return out; };
