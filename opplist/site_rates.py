"""The vendors' rates on a case, typed on the team site (batch Q2a, INTERFACE.md section 9): OppList's half.

The site saves a vendorRates/{id} document and a `rates` action of the
same id. OppList reads the document with the action, checks it, and keeps
it as a new version of that vendor's rates on the case (vendor_rate and
its lines, never over an older one; the same id twice is one version).
The case's rate request for that vendor is marked replied and quoted, or
made when there is none. The case page shows each vendor's newest rates
on its Rate requests row. Nothing is pushed back.
"""

from __future__ import annotations

import datetime as dt
import re

from . import db

SEGS = ("Freight", "Inland", "Origin", "Destination", "Customs", "Other")
WORDS = ("AT COST", "INCLUDED", "IF APPLICABLE", "N/A")
SAID = ("incl", "excl", "none", "notsaid")
SAID_FIELDS = (("fuel", "said_fuel", "Fuel"), ("local", "said_local", "Local charges"), ("min", "said_min", "Minimum"))
LINES_MAX = 40
SHORT_MAX = 60            # transit, carrier, free time
NOTE_MAX = 300
TEXT_MAX = 200            # a charge's name, a unit
LABEL_MAX = 60            # Q2e: an option's name
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Refused(Exception):
    """The rates do not fit; the message is for the person."""


# ------------------------------------------------------------- checks --
def _text(value, what, limit):
    if value is None:
        return None
    if not isinstance(value, str):
        raise Refused("{} is not text".format(what))
    value = " ".join(value.split())
    if len(value) > limit:
        raise Refused("{} is longer than {} characters".format(what, limit))
    return value or None


def _number(value, what):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused("{} is not a number".format(what))
    if value < 0:
        raise Refused("{} is below 0".format(what))
    return float(value)


def check(doc: dict) -> dict:
    """The document's values, checked (types, the lists, at most 40 lines,
    the lengths); raises Refused."""
    if not isinstance(doc, dict):
        raise Refused("the rates could not be read")
    cur = _text(doc.get("cur"), "the currency", 3)
    if not cur or not re.fullmatch(r"[A-Za-z]{3}", cur):
        raise Refused("the currency is not three letters")
    valid = doc.get("valid")
    if valid not in (None, ""):
        if not isinstance(valid, str) or not _DATE.match(valid):
            raise Refused("'{}' is not a date (valid until)".format(valid))
        try:
            dt.date.fromisoformat(valid)
        except ValueError:
            raise Refused("'{}' is not a date (valid until)".format(valid)) from None
    lines = doc.get("lines")
    if not isinstance(lines, list) or not lines:
        raise Refused("the rates have no lines")
    if len(lines) > LINES_MAX:
        raise Refused("{} lines, more than {}".format(len(lines), LINES_MAX))
    out_lines = []
    for n, line in enumerate(lines, start=1):
        what = "line {}".format(n)
        if not isinstance(line, dict):
            raise Refused("{} is not readable".format(what))
        seg = line.get("seg")
        if seg not in SEGS:
            raise Refused("{}: '{}' is not one of {}".format(what, seg, ", ".join(SEGS)))
        word = line.get("word")
        if word in ("", None):
            word = None
        elif word not in WORDS:
            raise Refused("{}: '{}' is not one of {}".format(what, word, ", ".join(WORDS)))
        out_lines.append({"seg": seg, "charge": _text(line.get("charge"), what + " charge", TEXT_MAX),
                          "unit": _text(line.get("unit"), what + " unit", TEXT_MAX),
                          "rate": _number(line.get("rate"), what + " rate"), "min": _number(line.get("min"), what + " min"),
                          "qty": _number(line.get("qty"), what + " qty"),
                          "amount": _number(line.get("amount"), what + " amount"), "word": word})
    said = doc.get("said") or {}
    if not isinstance(said, dict):
        raise Refused("what was said is not readable")
    out = {"cur": cur.upper(), "total": _number(doc.get("total"), "the total"), "valid_until": valid or None,
           "transit": _text(doc.get("transit"), "the transit time", SHORT_MAX),
           "carrier": _text(doc.get("carrier"), "the carrier", SHORT_MAX),
           "free_time": _text(doc.get("free"), "the free time", SHORT_MAX),
           "note": _text(doc.get("note"), "the note", NOTE_MAX), "lines": out_lines}
    for key, column, label in SAID_FIELDS:
        value = said.get(key)
        if value in (None, ""):
            value = None
        elif value not in SAID:
            raise Refused("{}: '{}' is not one of {}".format(label, value, ", ".join(SAID)))
        out[column] = value
    return out


# ------------------------------------------------------------ the vendor --
def vendor_of(conn, case_id, key):
    """The vendor's party: one of lists/vendors (firebase_vendors), or one
    of the case's rate requests; a merged party is the one it went into.
    None when it is neither."""
    from . import firebase_pull, firebase_vendors
    key = " ".join(str(key or "").split())
    if not key:
        return None
    found = conn.execute("SELECT id FROM party WHERE lower(trim(key)) = lower(?) ORDER BY id", (key,)).fetchone()
    party = firebase_pull._kept(conn, found["id"]) if found else None
    if party is None:
        return None
    if party["id"] in firebase_vendors._vendors(conn):
        return party
    asked = conn.execute("SELECT 1 FROM vendor_request WHERE enquiry_id = ? AND vendor_party_id = ? AND voided = 0",
                         (case_id, party["id"])).fetchone()
    return party if asked else None


# ----------------------------------------------------------- the action --
def in_lists(conn, case) -> bool:
    from . import firebase_sync
    return conn.execute("SELECT 1 FROM v_enquiry e WHERE e.id = ? AND {}".format(firebase_sync._scope_sql("e")),
                        (case["id"],)).fetchone() is not None


def _short_day(day) -> str:
    try:
        return dt.date.fromisoformat(day).strftime("%b %d").replace(" 0", " ")
    except (TypeError, ValueError):
        return day or "-"


def label_key(label) -> str:
    """Q2e: labels are compared without regard to case or extra spaces."""
    return " ".join(str(label or "").split()).casefold()


def label_of(doc, action) -> str:
    """Q2e: the option's name (the document's, else the action's), trimmed
    and kept as typed; '' for the vendor's one set; raises Refused."""
    value = doc.get("label", action.get("label")) if isinstance(doc, dict) else action.get("label")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Refused("the option's name is not text")
    value = " ".join(value.split())
    if len(value) > LABEL_MAX:
        raise Refused("the option's name is {} characters, more than {}".format(len(value), LABEL_MAX))
    return value


def message(opp, vendor, values) -> str:
    """'OPP-1642: rates from <vendor> kept (USD 2,350, valid until Nov 30).'
    Q2e: with a label, 'rates from <vendor>, Partial, kept (...)'."""
    from . import cargo
    bits = []
    if values.get("total") is not None:
        bits.append(cargo.value_text(values["total"], values["cur"]))
    if values.get("valid_until"):
        bits.append("valid until {}".format(_short_day(values["valid_until"])))
    label = values.get("label") or ""
    return "{}: rates from {}{} kept{}.".format(opp, vendor, ", " + label + "," if label else "",
                                                " ({})".format(", ".join(bits)) if bits else "")


def take(repo, case, action, who, today, action_id, client) -> str:
    """The action 'rates' (section 9 points 1 to 4); raises Refused."""
    from .firebase_pull import SOURCE, iso
    conn = repo.conn
    opp = case["opp_no"]
    if not in_lists(conn, case):
        raise Refused("{} is not in the lists (scope Out, a duplicate or registered in error), so the rates were not "
                      "kept.".format(opp))
    party = vendor_of(conn, case["id"], action.get("vendor"))
    if party is None:
        raise Refused("{}: {} is not a vendor in OppList, so the rates were not kept.".format(
            opp, action.get("vendor") or "the vendor"))
    known = conn.execute("SELECT id FROM vendor_rate WHERE site_id = ?", (action_id,)).fetchone()
    if known is not None:
        row = conn.execute("SELECT * FROM vendor_rate WHERE id = ?", (known["id"],)).fetchone()
        return message(opp, party["key"], dict(row))                  # the same save: one version
    doc = None
    try:
        doc = client.get("vendorRates/" + action_id) if client is not None else None
    except Exception:
        doc = None
    if not doc:
        raise Refused("{}: the rates from {} could not be read (vendorRates/{} is not there), so they were not "
                      "kept.".format(opp, party["key"], action_id))
    if str(doc.get("opp") or "").strip().upper() != opp or \
            " ".join(str(doc.get("vendor") or "").split()).casefold() != " ".join(str(action.get("vendor") or "").split()).casefold():
        raise Refused("{}: the rates document is for another case or vendor, so it was not kept.".format(opp))
    try:
        values = check(doc)
        values["label"] = label_of(doc, action)
    except Refused as exc:
        raise Refused("{}: {}, so the rates from {} were not kept.".format(opp, exc, party["key"])) from None
    at = iso(doc.get("at") or action.get("at"))
    # Q2b: the route the rates are for, and the day the vendor sent them
    live = conn.execute("SELECT id, seq FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq",
                        (case["id"],)).fetchall()
    route = doc.get("route", action.get("route"))
    route_id = None
    if route not in (None, ""):
        try:
            seq = int(route)
        except (TypeError, ValueError):
            seq = None
        found = [r for r in live if r["seq"] == seq]
        if not found:
            raise Refused("{}: route {} is not a route of the case, so the rates from {} were not kept.".format(
                opp, route, party["key"]))
        route_id = found[0]["id"]
    elif len(live) == 1:
        route_id = live[0]["id"]
    on = doc.get("on", action.get("on"))
    if on not in (None, ""):
        if not isinstance(on, str) or not _DATE.match(on):
            raise Refused("{}: '{}' is not a date, so the rates from {} were not kept.".format(opp, on, party["key"]))
        try:
            dt.date.fromisoformat(on)
        except ValueError:
            raise Refused("{}: '{}' is not a date, so the rates from {} were not kept.".format(
                opp, on, party["key"])) from None
        if on > today:
            raise Refused("{}: {} is after today, so the rates from {} were not kept.".format(opp, on, party["key"]))
        day = on
    else:
        day = _local_day(at) or today
    # Q2d: saved from a Read quote draft (draft, option, how 'read'), or typed
    from . import read_quote
    how, draft, first, linked = read_quote.resolve(conn, case, party, doc)
    by_name = str(doc.get("name") or action.get("name") or "") or None
    rate_id = repo.insert("vendor_rate", {
        "enquiry_id": case["id"], "vendor_party_id": party["id"], "site_id": action_id, "at": at,
        "by_email": str(doc.get("by") or action.get("by") or "") or None, "by_name": by_name,
        **{k: v for k, v in values.items() if k != "lines"}, "source": SOURCE,
        "route_id": route_id, "received_on": day, "how": how, "draft_id": draft["site_id"] if draft else None})
    for n, line in enumerate(values["lines"], start=1):
        repo.insert("vendor_rate_line", dict(line, rate_id=rate_id, seq=n))
    if draft is not None and first:
        seq = next((r["seq"] for r in live if r["id"] == route_id), None)
        read_quote.mark_saved(repo, draft, rate_id, doc, values, seq, day, by_name or who, at)
    _mark_request(repo, case, party, day, who, route_id, len(live))
    return message(opp, party["key"], values) + linked


def _local_day(at) -> str:
    """The day of a UTC moment where she works (the machine's local time)."""
    try:
        moment = dt.datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return ""
    return moment.astimezone().date().isoformat()


def _covers(request, route_id) -> bool:
    from . import routes as routes_mod
    ids = routes_mod.parse_ids(request["route_ids"])
    return route_id is None or not ids or route_id in ids


def _mark_request(repo, case, party, day, who, route_id=None, routes_live=1) -> None:
    """Point 3: the vendor's newest live rate request that covers the route
    (its routes include it, or it covers every route) is replied and quoted
    on the day, when it is not already. Q2b: when the vendor has live
    requests but none covers the route, one is made for that route; with
    none at all, one is made (for that route on a case with several live
    routes) - asked and quoted that day."""
    conn = repo.conn
    from .firebase_pull import SOURCE
    requests = conn.execute("SELECT * FROM vendor_request WHERE enquiry_id = ? AND vendor_party_id = ? AND voided = 0 "
                            "ORDER BY asked_on DESC, id DESC", (case["id"], party["id"])).fetchall()
    request = next((r for r in requests if _covers(r, route_id)), None)
    if request is None:
        for_route = route_id is not None and (requests or routes_live > 1)
        repo.insert("vendor_request", {
            "enquiry_id": case["id"], "vendor_party_id": party["id"], "asked_on": day, "replied_on": day,
            "machine_status": "Quoted", "source": SOURCE, "route_ids": str(route_id) if for_route else None,
            "notes": "Rates typed on the team site by {}".format(who)})
        repo.confirm_status(case["id"])
        return
    values = {}
    if not request["replied_on"]:
        values["replied_on"] = day
    if request["machine_status"] != "Quoted":
        values["machine_status"] = "Quoted"
    if values:
        repo.update("vendor_request", request["id"], values)


# ---------------------------------------------------------- the case page --
def card(conn, case_id, today=None) -> dict:
    """{vendor party id: [the newest rates of each route]} for the Rate
    requests card (Q2b: per vendor and route): the total and currency,
    valid until (past or not), Lowest among the case's vendors on that
    route in that currency, what was not said, the lines, who and when, the
    day the vendor sent them, and how many older versions."""
    from . import cargo
    today = today or dt.date.today().isoformat()
    if not db._has_table(conn, "vendor_rate"):
        return {}
    from . import routes as routes_mod
    letters = {r["id"]: routes_mod.letter(r["seq"]) for r in conn.execute(
        "SELECT id, seq FROM enquiry_route WHERE enquiry_id = ? AND voided = 0", (case_id,))}
    # Q2b: the newest rates per vendor and route; Lowest per route and currency
    newest = {}
    older = {}
    struck = {}                 # Q2c: the versions struck out, for Put back
    labels = {}                 # Q2e: each option's label as typed in its newest version
    for r in conn.execute("SELECT * FROM vendor_rate WHERE enquiry_id = ? ORDER BY at, id", (case_id,)):
        # Q2e: per vendor, route and option (the label without case or extra spaces)
        key = (r["vendor_party_id"], r["route_id"], label_key(_label(r)))
        labels[key] = _label(r)
        if r["voided"]:
            struck.setdefault(key, []).append({"id": r["id"], "at": _local_stamp(r["at"]),
                                               "total": cargo.value_text(r["total"], r["cur"]) if r["total"] is not None else "",
                                               "by": r["by_name"] or (r["by_email"] or "").split("@")[0]})
            continue
        if key in newest:
            older[key] = older.get(key, 0) + 1
        newest[key] = r
    lowest_ids = lowest_of(newest.values(), today)
    out = {}
    order = {}                  # Q2e: a vendor's options in the order first saved
    for n, ((party_id, route_id, lkey), r) in enumerate(newest.items()):
        order.setdefault((party_id, route_id, lkey), n)
        lines = conn.execute("SELECT * FROM vendor_rate_line WHERE rate_id = ? ORDER BY seq", (r["id"],)).fetchall()
        key = (party_id, route_id, lkey)
        out.setdefault(party_id, []).append({
            "route_id": route_id, "letter": letters.get(route_id, "") if len(letters) > 1 else "",
            "label": labels.get(key, ""), "order": order[key],
            "received": r["received_on"] or "",
            "id": r["id"], "total": cargo.value_text(r["total"], r["cur"]) if r["total"] is not None else "",
            "valid": r["valid_until"] or "", "past": bool(r["valid_until"] and r["valid_until"] < today),
            "lowest": r["id"] in lowest_ids,
            "not_said": ["{} not said".format(label) for _key, column, label in SAID_FIELDS if r[column] == "notsaid"],
            "transit": r["transit"] or "", "carrier": r["carrier"] or "", "free": r["free_time"] or "",
            "note": r["note"] or "", "how": r["how"] if "how" in r.keys() else "web", "by": r["by_name"] or (r["by_email"] or "").split("@")[0] or "the team site",
            "at": _local_stamp(r["at"]), "older": older.get(key, 0),
            "struck": struck.get(key, []),
            "lines": [{"seg": l["seg"], "charge": l["charge"] or "", "unit": l["unit"] or "",
                       "rate": _plain(l["rate"]), "min": _plain(l["min"]), "qty": _plain(l["qty"]),
                       "amount": l["word"] or _plain(l["amount"], 2)} for l in lines]})
    # Q2c: a vendor's route whose every version is struck out: only Put back
    for n, ((party_id, route_id, lkey), versions) in enumerate(struck.items()):
        if (party_id, route_id, lkey) not in newest:
            out.setdefault(party_id, []).append({
                "route_id": route_id, "letter": letters.get(route_id, "") if len(letters) > 1 else "",
                "label": labels.get((party_id, route_id, lkey), ""), "order": len(newest) + n,
                "id": None, "struck": versions, "lines": [], "total": "", "not_said": []})
    for entries in out.values():
        entries.sort(key=lambda e: (e["letter"] or "", e["route_id"] or 0, e["order"]))
    return out


def _label(row) -> str:
    """Q2e: a version's label ('' before v55: the vendor's one set)."""
    return (row["label"] or "") if "label" in row.keys() else ""


def lowest_of(rows, today) -> set:
    """Q2e (INTERFACE.md section 13): the ids of the current options that
    are Lowest - on a route, among every vendor's current options in one
    currency, two or more of them, leaving out those whose valid until is
    before today: the lowest total; a tie marks each."""
    groups = {}
    for r in rows:
        if r["total"] is None or (r["valid_until"] and r["valid_until"] < today):
            continue
        groups.setdefault((r["route_id"], r["cur"]), []).append(r)
    out = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        low = min(r["total"] for r in group)
        out.update(r["id"] for r in group if abs(r["total"] - low) < 1e-9)
    return out


def strike(repo, rate_id: int, voided: bool = True) -> str:
    """Q2c: a version typed by mistake struck out (or put back) through the
    repository - change_log has it, Undo works as for other strikes. The
    newest version not struck out is the vendor's rates again, on the case
    page, on the site (cases/{OPP}.rx) and on the Rate Card (rev,
    withdrawn)."""
    row = repo.conn.execute("SELECT r.*, p.key FROM vendor_rate r JOIN party p ON p.id = r.vendor_party_id "
                            "WHERE r.id = ?", (rate_id,)).fetchone()
    if row is None:
        raise ValueError("that version is not on file")
    repo.update("vendor_rate", rate_id, {"voided": 1 if voided else 0})
    repo.commit()
    return "Rates from {} ({}){}".format(row["key"], _local_stamp(row["at"]),
                                         " struck out." if voided else " put back.")


def for_request(card_rows, request, live_routes) -> list:
    """Q2b: the rates a Rate requests row shows - the vendor's newest rates
    of each route the request covers, and those that name no route."""
    from . import routes as routes_mod
    covered = {r["id"] for r in routes_mod.applies_to(request["route_ids"], live_routes)}
    return [e for e in card_rows.get(request["vendor_party_id"], [])
            if e["route_id"] is None or e["route_id"] in covered]


def _plain(value, places=None) -> str:
    if value is None:
        return ""
    if places is not None:
        return "{:,.2f}".format(value).rstrip("0").rstrip(".") if value != int(value) else "{:,.0f}".format(value)
    return "{:g}".format(value)


def _local_stamp(at) -> str:
    try:
        moment = dt.datetime.strptime((at or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return (at or "")[:16]
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")
