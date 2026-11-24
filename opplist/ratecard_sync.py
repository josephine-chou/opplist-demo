"""The vendors' rates kept in OppList (Q2a, Q2b) pushed to the Rate Card's
Agent quotes (batch Q2c, INTERFACE.md section 10).

The Rate Card is a document store of its own (setting ratecard_project,
default demo-rate-card); OppList writes its collection agentQuotes. In
this edition that store is a folder of JSON documents under the data folder
(local_store), switched on with setting ratecard_store: no service, no key.
Nothing else of that store is written, nothing is read from it, nothing
is ever deleted. Only vendors' costs go: never our markup, our rates, our
quotes or the requester.

One document per quote: the versions of one vendor's rates for one route
of one case, in the order they were saved, make its quotes - the first
starts quote 1; a version whose day differs from the one before starts the
next; one with the same day corrects that quote. A document is written
whole when its content (all but pushedAt) differs from what was last
written, at most 400 a commit and a run; ..\\data\\ratecard-sync.json
keeps each quote's id (never recomputed once written), the hashes, the
time, the counts and the last error. It runs in the team site's sync run,
after meta/sync, only over the live database with the store switched on; its
failure is its own.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import cargo, db, paths, routes as routes_mod

COLLECTION = "agentQuotes"
PROJECT_SETTING = "ratecard_project"
DEFAULT_PROJECT = "demo-rate-card"
BATCH = 400
SCHEMA = 1
STRUCK_NOTE = "Struck out in OppList"
#: a mode as the Rate Card names it
MODES = {"FCL": "FCL", "LCL": "LCL", "Air": "Air", "Truck": "Truck", "Cross-border Truck": "Truck"}
SCOPES = {"Door to door": "D2D", "Door to port": "D2P", "Port to door": "P2D", "Port to port": "P2P"}
KINDS = {"Agent": "agent", "Office": "office", "Carrier": "carrier", "Coloader": "coloader",
         "Trucker": "trucker"}
CATEGORIES = {"Freight": "freight", "Inland": "inland", "Origin": "origin", "Destination": "dest",
              "Customs": "customs", "Other": "other"}
NOT_SAID = (("said_fuel", "fuel"), ("said_local", "destination"), ("said_min", "minimum"))
#: the four container types the Rate Card knows, as it writes them
EQUIPMENT = {"20": "20'", "20GP": "20'", "20DC": "20'", "40": "40'", "40GP": "40'", "40DC": "40'",
             "40HQ": "40'HQ", "40HC": "40'HQ", "45HQ": "45'HQ", "45HC": "45'HQ", "45": "45'HQ"}
UNITS = {"KG": "per_kg", "CBM": "per_cbm", "W/M": "per_wm", "WM": "per_wm", "SHIPMENT": "per_shipment",
         "SHPT": "per_shipment", "BL": "per_bl", "HBL": "per_bl", "HAWB": "per_bl", "AWB": "per_bl"}


class RateCardError(Exception):
    """No key, the wrong project, the network, the API."""


# ------------------------------------------------------------- settings --
#: This edition keeps the Rate Card's documents in a folder of JSON files
#: (local_store), never in a service: setting ratecard_store 'on' turns the
#: push on (off by default; the demo turns it on).
STORE_SETTING = "ratecard_store"
STORE_FOLDER = "rate-card"
OFF_LINE = "the Rate Card's local store is off (Settings, Team site)"


def store_dir(conn=None) -> Path:
    return paths.DATA_DIR / STORE_FOLDER


def project(conn) -> str:
    return (db.get_setting(conn, PROJECT_SETTING, DEFAULT_PROJECT) or "").strip() or DEFAULT_PROJECT


def service_account(conn) -> str:
    """What the push writes to, as Settings shows it; '' while it is off."""
    return "local store in {}".format(store_dir(conn)) if configured(conn)[0] else ""


def configured(conn) -> tuple:
    """(True, '') when the push can go, else (False, why)."""
    on = (db.get_setting(conn, STORE_SETTING, "") or "").strip().lower() in ("on", "1", "yes", "true")
    return (True, "") if on else (False, OFF_LINE)


def client_for(conn):
    from . import firebase_sync, local_store
    ok, why = configured(conn)
    if not ok:
        raise RateCardError(why)
    return local_store.LocalStore(project(conn), store_dir(conn), error=firebase_sync.FirebaseError)


# --------------------------------------------------------------- the id --
def vendor_slug(key) -> str:
    """'Wide Freight Ltd.' -> 'wide-freight-ltd': lower case, each run of
    characters other than a to z and 0 to 9 one '-'."""
    return re.sub(r"[^a-z0-9]+", "-", (key or "").lower()).strip("-") or "vendor"


# ---------------------------------------------------------- the content --
def _place(conn, location_id, words, country) -> dict:
    """{key, name, country, unlocode, iata}: the master location, or the
    words as written."""
    if location_id:
        row = conn.execute("SELECT key, name, country, unlocode, iata FROM master_location WHERE id = ?",
                           (location_id,)).fetchone()
        if row is not None:
            return {"key": row["key"], "name": row["name"] or row["key"],
                    "country": "" if (row["country"] or "") == "Unknown" else (row["country"] or ""),
                    "unlocode": row["unlocode"] or "", "iata": row["iata"] or ""}
    return {"key": "", "name": words or "", "country": "" if (country or "") == "Unknown" else (country or ""),
            "unlocode": "", "iata": ""}


def _first_pod(conn, route_id):
    if not route_id or not db._has_table(conn, "enquiry_route_place"):
        return None
    row = conn.execute("SELECT location_id FROM enquiry_route_place WHERE route_id = ? AND side = 'pod' AND voided = 0 "
                       "ORDER BY seq LIMIT 1", (route_id,)).fetchone()
    return row["location_id"] if row else None


def _equipment(text) -> str:
    plain = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    return EQUIPMENT.get(plain, "")


def _cargo(conn, case, route) -> dict:
    """The route's cargo as the case page's Cargo card counts it
    (routes.totals and routes.applies_to: each line once per route it
    covers)."""
    live = routes_mod.routes_of(conn, case["id"])
    totals = routes_mod.totals(conn, case["id"], live_routes=live)
    row = next((r for r in totals["per_route"] if route is not None and r["route"]["id"] == route["id"]),
               totals["per_route"][0] if len(totals["per_route"]) == 1 else None)
    mode = (route["service_mode"] if route is not None else None) or case["service_mode"]
    lines = [l for l in cargo.lines_of(conn, case["id"]) if int(l.get("counted", 1) or 0)]
    covering = [l for l in lines
                if route is None or any(r["id"] == route["id"] for r in routes_mod.applies_to(l.get("route_ids"), live))]
    out = {}
    if mode == "FCL":
        boxes = {}
        for l in covering:
            if l.get("container_qty"):
                eq = _equipment(l.get("container_type")) or (l.get("container_type") or "")
                boxes[eq] = boxes.get(eq, 0) + int(l["container_qty"])
        out["containers"] = [{"eq": eq, "qty": qty} for eq, qty in boxes.items()]
    elif mode == "LCL":
        out["cbm"] = round(row["volume"], 3) if row and row["volume"] is not None else None
        out["kg"] = round(row["gross"], 1) if row and row["gross"] is not None else None
    elif mode in cargo.AIR_MODES:
        pieces = [cargo.derive(l)["pieces_n"] for l in covering]
        out["pieces"] = sum(p for p in pieces if p) if any(pieces) else None
        out["kg"] = round(row["gross"], 1) if row and row["gross"] is not None else None
        charge = routes_mod.chargeable(row["gross"], row["volume"]) if row else None
        out["chargeableKg"] = round(charge, 1) if charge is not None else None
    out["commodity"] = cargo.commodity_of(covering) or case["commodity"] or ""
    out["dg"] = "dg" in {t.strip().casefold() for t in (case["special_handling"] or "").replace(",", ";").split(";")}
    return out


def _unit(unit) -> tuple:
    """(unit, equipment, words after the charge) from the line's unit."""
    text = " ".join((unit or "").split())
    upper = text.upper()
    eq = _equipment(text)
    if eq:
        return "per_container", eq, ""
    if upper in ("CNTR", "CONTAINER", "CTR"):
        return "per_container", "", ""
    if upper in UNITS:
        return UNITS[upper], "", ""
    return "other", "", " (per {})".format(text) if text else ""


def _lines(conn, rate_id) -> list:
    out = []
    for l in conn.execute("SELECT * FROM vendor_rate_line WHERE rate_id = ? ORDER BY seq", (rate_id,)):
        unit, eq, after = _unit(l["unit"])
        charge = (l["charge"] or "") + after
        word = l["word"]
        if word:
            charge += " ({})".format(word)
        item = {"charge": charge, "category": CATEGORIES.get(l["seg"], "other"), "unit": unit,
                "rate": None if word else l["rate"], "min": l["min"], "qty": l["qty"],
                "amount": None if word else l["amount"]}
        if eq:
            item["equipment"] = eq
        out.append(item)
    return out


def _case_status(status) -> str:
    return status if status in ("New", "Quoted", "Won", "Lost") else "Closed"


def _closed_note(case) -> str:
    reason = case["closed_reason"] or ""
    if reason.startswith("Duplicate"):
        return "The case was a duplicate"
    if reason == "Registered in error":
        return "The case was registered in error"
    return ""


@dataclass
class Quote:
    case_id: int
    party_id: int
    route_id: int | None
    seq: int
    versions: list = field(default_factory=list)       # vendor_rate rows, in the order saved
    label: str = ""                                    # Q2e: the option ('' = the vendor's one set)

    @property
    def option(self) -> str:
        from . import site_rates
        return site_rates.label_key(self.label)

    @property
    def group(self) -> str:
        # Q2e: an option's group carries its label; one set keeps the ids written before
        if self.option:
            return "{}:{}:{}:o{}:{}".format(self.case_id, self.party_id, self.route_id or "", self.option, self.seq)
        return "{}:{}:{}:{}".format(self.case_id, self.party_id, self.route_id or "", self.seq)

    @property
    def live(self) -> list:
        return [v for v in self.versions if not v["voided"]]


def quotes(conn) -> list:
    """Every quote: the versions of one vendor's rates for one route of one
    case, cut where the day changes from the version before."""
    out = []
    if not db._has_table(conn, "vendor_rate"):
        return out
    from . import site_rates
    current = {}
    for v in conn.execute("SELECT * FROM vendor_rate ORDER BY enquiry_id, vendor_party_id, COALESCE(route_id, 0), at, id"):
        # Q2e: one quote per option - the seq and the day rule per option
        label = site_rates._label(v)
        key = (v["enquiry_id"], v["vendor_party_id"], v["route_id"], site_rates.label_key(label))
        day = v["received_on"] or (v["at"] or "")[:10]
        q = current.get(key)
        if q is None:
            q = Quote(v["enquiry_id"], v["vendor_party_id"], v["route_id"], 1, label=label)
            out.append(q)
        elif day != (q.versions[-1]["received_on"] or (q.versions[-1]["at"] or "")[:10]):
            q = Quote(v["enquiry_id"], v["vendor_party_id"], v["route_id"], q.seq + 1, label=label)
            out.append(q)
        q.versions.append(v)
        q.label = label                     # as typed in its newest version
        current[key] = q
    return out


def label_slug(label) -> str:
    """Q2e: an option's part of the id, made like the vendor's ('' for one set)."""
    if not " ".join(str(label or "").split()):
        return ""
    return re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-") or "option"


def _timestamp(at):
    from . import firebase_sync
    try:
        moment = dt.datetime.strptime((at or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return firebase_sync.Timestamp(moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def _how(row) -> str:
    """Q2d: the version's how: 'read' when it was saved from a Read quote draft."""
    return "read" if "how" in row.keys() and row["how"] == "read" else "web"


def build(conn, ids: dict) -> dict:
    """{document id: content (without pushedAt)} for every quote. `ids`
    ({quote group: id}) is the state's: an id once written stays; a new
    quote gets one here (and `ids` keeps it)."""
    from . import site_rates
    all_quotes = quotes(conn)
    # usedOnWon: the awarded vendor's newest quote of each option (Q2e) on each route not Lost or Cancelled
    newest = {}
    for q in all_quotes:
        if q.live:
            k = (q.case_id, q.party_id, q.route_id, q.option)
            if k not in newest or q.seq > newest[k]:
                newest[k] = q.seq
    out = {}
    for q in all_quotes:
        case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (q.case_id,)).fetchone()
        party = conn.execute("SELECT * FROM party WHERE id = ?", (q.party_id,)).fetchone()
        if case is None or party is None:
            continue
        route = conn.execute("SELECT * FROM enquiry_route WHERE id = ?", (q.route_id,)).fetchone() if q.route_id else None
        live = routes_mod.routes_of(conn, q.case_id)
        if route is None and len(live) == 1 and q.route_id is None:
            route = conn.execute("SELECT * FROM enquiry_route WHERE id = ?", (live[0]["id"],)).fetchone()
        letter = routes_mod.letter(route["seq"]) if route is not None else ""
        doc_id = ids.get(q.group)
        if not doc_id:
            # Q2e: the option's label after the vendor; '' keeps the ids written before
            doc_id = "_".join(x for x in (case["opp_no"], letter or "all", vendor_slug(party["key"]),
                                          label_slug(q.label), str(q.seq)) if x)
            ids[q.group] = doc_id
        shown = q.live[-1] if q.live else q.versions[-1]
        withdrawn_note = STRUCK_NOTE if not q.live else _closed_note(case)
        mode = MODES.get((route["service_mode"] if route is not None else None) or case["service_mode"] or "", "Other")
        doc = {
            "schema": SCHEMA, "opp": case["opp_no"], "route": letter, "seq": q.seq, "rev": len(q.live),
            "receivedOn": shown["received_on"] or site_rates._local_day(shown["at"]) or (shown["at"] or "")[:10],
            "loggedAt": _timestamp(shown["at"]),
            "loggedBy": shown["by_name"] or (shown["by_email"] or "").split("@")[0],
            "how": _how(shown), "confirmedBy": (shown["by_name"] or "") if _how(shown) == "read" else "",
            "mode": mode,
            "origin": _place(conn, route["origin_loc_id"] if route is not None else None,
                             (route["origin_loc"] or route["origin_alt"]) if route is not None else case["origin_loc"],
                             route["origin_country"] if route is not None else case["origin_country"]),
            "destination": _place(conn, route["dest_loc_id"] if route is not None else None,
                                  (route["dest_loc"] or route["dest_alt"]) if route is not None else case["dest_loc"],
                                  route["dest_country"] if route is not None else case["dest_country"]),
            "scope": SCOPES.get((route["service_scope"] if route is not None else None) or "", ""),
            "incoterm": (route["incoterm"] if route is not None else None) or case["incoterm"] or "",
            "cargo": _cargo(conn, case, route),
            "vendor": {"key": party["key"], "name": party["key"],
                       "country": "" if (party["country"] or "") == "Unknown" else (party["country"] or ""),
                       "kind": KINDS.get(party["type"], "other")},
            "carrier": shown["carrier"] or "", "transit": shown["transit"] or "", "freeTime": shown["free_time"] or "",
            "validUntil": shown["valid_until"] or "", "routing": q.label, "validNote": "",
            "currency": shown["cur"] or "", "total": shown["total"], "lines": _lines(conn, shown["id"]),
            "notSaid": [word for column, word in NOT_SAID if shown[column] == "notsaid"],
            "notes": shown["note"] or "", "caseStatus": _case_status(case["status"]),
            "usedOnWon": bool(case["status"] == "Won" and case["awarded_vendor_id"] == q.party_id and q.live
                              and newest.get((q.case_id, q.party_id, q.route_id, q.option)) == q.seq
                              and (route is None or route["result"] not in ("Lost", "Cancelled"))),
            "withdrawn": bool(withdrawn_note), "withdrawnNote": withdrawn_note,
        }
        if route is not None:
            pod = _first_pod(conn, route["id"])
            if pod:
                doc["pod"] = _place(conn, pod, "", route["dest_country"])
        out[doc_id] = doc
    return out


# --------------------------------------------------------------- the push --
def state_path() -> Path:
    return paths.DATA_DIR / "ratecard-sync.json"


def load_state(path=None) -> dict:
    from . import firebase_sync
    return firebase_sync._load_json(path or state_path())


def save_state(state: dict, path=None) -> None:
    from . import firebase_sync
    firebase_sync._save_json(state, path or state_path())


@dataclass
class PushResult:
    ok: bool = False
    off: str = ""                   # why nothing was pushed (no key, another project)
    written: int = 0
    unchanged: int = 0
    left: int = 0                   # over the 400 of a run: the next run
    quotes: int = 0
    error: str = ""

    def line(self) -> str:
        if self.off:
            return "Rate Card: off: {}".format(self.off)
        if not self.ok:
            return "Rate Card: failed: {}".format(self.error)
        return "Rate Card: {} document{} written, {} agent quote{} in all{}".format(
            self.written, "" if self.written == 1 else "s", self.quotes, "" if self.quotes == 1 else "s",
            "; {} left for the next run".format(self.left) if self.left else "")


def push(conn, client, state: dict) -> PushResult:
    """Write the agentQuotes documents whose content changed, whole, with
    pushedAt the server's time; at most 400 a commit and a run. `state` is
    updated as each commit lands."""
    from . import firebase_sync
    result = PushResult()
    ids = state.setdefault("ids", {})
    hashes = state.setdefault("hashes", {})
    docs = build(conn, ids)
    result.quotes = len(docs)
    writes = []
    for doc_id, doc in docs.items():
        h = firebase_sync.digest(doc)
        if hashes.get(doc_id) == h:
            result.unchanged += 1
            continue
        writes.append((doc_id, doc, h))
    result.left = max(len(writes) - BATCH, 0)
    writes = writes[:BATCH]
    if writes:
        client.commit([firebase_sync.Write("{}/{}".format(COLLECTION, doc_id), doc, server_time=("pushedAt",))
                       for doc_id, doc, _h in writes])
        for doc_id, _doc, h in writes:
            hashes[doc_id] = h
            result.written += 1
    result.ok = True
    return result


def run(conn, client=None, db_path=None, state_file=None) -> PushResult:
    """One push with its own state file; never raises. Without a client:
    only over the live database with the store switched on."""
    from . import firebase_sync, logs
    state_file = state_file or state_path()
    state = load_state(state_file)
    result = PushResult()
    try:
        if client is None:
            if not firebase_sync.is_live(db_path or paths.DB_PATH):
                result.off = "not the live database; only the live app pushes"
                return result
            ok, why = configured(conn)
            if not ok:
                result.off = why
                _note(state, result, state_file)
                return result
            client = client_for(conn)
        name = getattr(client, "project", None) or project(conn)
        if state.get("project") not in (None, name):
            # another project: everything again, the ids kept
            state = {"project": name, "ids": state.get("ids") or {}}
        state["project"] = name
        result = push(conn, client, state)
        logs.get("firebase").info("rate card pushed: %s", result.line())
    except Exception as exc:                            # its failure is its own
        result.ok = False
        result.error = str(exc) if isinstance(exc, (RateCardError, firebase_sync.FirebaseError)) else \
            "{}: {}".format(type(exc).__name__, exc)
        logs.get("firebase").warning("rate card push failed: %s", result.error)
    _note(state, result, state_file)
    return result


def _note(state, result, state_file) -> None:
    state.update({"last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "last_ok": result.ok,
                  "last_off": result.off, "last_error": result.error, "last_written": result.written,
                  "last_quotes": result.quotes, "last_left": result.left})
    try:
        save_state(state, state_file)
    except OSError:
        pass


def status_line(conn, state_file=None) -> str:
    """Settings' Rate Card line and firebase-sync --status: the last push
    (when, documents written, agent quotes in all), or its error, or 'Off:
    <why>'."""
    ok, why = configured(conn)
    if not ok:
        return "Off: {}".format(why)
    state = load_state(state_file)
    if not state.get("last_run"):
        return "Set up ({}); not pushed yet".format(service_account(conn) or "?")
    if state.get("last_off"):
        return "Off: {}".format(state["last_off"])
    if not state.get("last_ok"):
        return "{}: failed: {}".format(state["last_run"], state.get("last_error") or "?")
    return "{}: {} document{} written, {} agent quote{} in all".format(
        state["last_run"], state.get("last_written", 0), "" if state.get("last_written", 0) == 1 else "s",
        state.get("last_quotes", 0), "" if state.get("last_quotes", 0) == 1 else "s")
