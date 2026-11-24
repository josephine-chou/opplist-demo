"""The team site's quote card's quotes, taken into OppList (batch Q3, schema
v56, INTERFACE.md section 7).

Since 1111d the card saves each quote as quotes/{id} (the sell side) and,
when a cost was typed, quoteCosts/{id} (the costs), under the id of its
quote action; W3 takes the action in as Quoted or a re-quote with
"[quote:<id>]" at the end of the event's note. Every sync run, after the
actions (firebase_pull.pull, the scheduled run's included), take_quotes()
reads the two documents for each live event carrying a mark whose quote
OppList does not have yet, oldest event first, at most 50 a run, and
writes the quote and its lines in one transaction, linked to the event.
A document missing, of another case or failing the field rules is not
written and is tried again at the next run; the log says so once a day.
Nothing is written to the site.

The case page reads view() (its Quotes card), marks() and note_html() (the
timeline's link), and won_start() (the Won form's Rate used, from the last
quote's Freight part).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re

from markupsafe import Markup, escape

from . import db, logs, parsing

#: a mark in an event's note, as W3 writes it (firebase_pull._QUOTE_ID)
MARK = re.compile(r"\[quote:([A-Za-z0-9_-]{1,128})\]")
#: any mark, hidden on a page when its quote is not in OppList
_ANY_MARK = re.compile(r"\s*\[quote:[^\]]*\]")
RUN_MAX = 50
LINES_MAX = 40
TERMS_MAX = 12
SOURCES_MAX = 6
#: the site's limits (firestore.rules and quotemail.quoteDoc); text is cut at them
CUT = {"subject": 300, "pdf": 80, "rateFrom": 80, "transit": 100, "routing": 200, "remarks": 1000, "term": 1000,
       "charge": 80, "qty": 20, "unit": 20, "word": 40, "remark": 200, "seg": 20, "key": 80, "name": 80,
       "carrier": 40, "label": 60, "by": 200}
SEGS = ("Freight", "Inland", "Origin", "Destination", "Customs", "Other")
STATE_KEY = "quotes_told"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CUR = re.compile(r"^[A-Z]{3}$")


class Bad(Exception):
    """The documents do not fit section 7; not written, tried again."""


# ------------------------------------------------------------- checks --
def _cut(value, what):
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()[:CUT[what]].strip()
    return value or None


def _number(value, what):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Bad("{} is not a number".format(what))
    return float(value)


def _day(value, what, required=True):
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not _DAY.match(value):
        raise Bad("{} is not a date".format(what))
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise Bad("{} is not a date".format(what)) from None
    return value


def _sources(value) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for part in value[:SOURCES_MAX]:
        if not isinstance(part, dict):
            continue
        item = {k: _cut(part.get(k), k) or "" for k in ("seg", "key", "name", "carrier")}
        if part.get("label"):
            item["label"] = _cut(part.get("label"), "label") or ""
        out.append(item)
    return out


def check(doc, costs, opp) -> dict:
    """The quote's values from quotes/{id} and quoteCosts/{id} (None: no
    costs), checked as section 7 says; raises Bad."""
    from .firebase_pull import iso
    if not isinstance(doc, dict):
        raise Bad("quotes/{id} is not there")
    if str(doc.get("opp") or "").strip().upper() != opp:
        raise Bad("the quote is for another case ({})".format(doc.get("opp") or "none"))
    n = doc.get("n")
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if isinstance(n, bool) or not isinstance(n, int) or not 0 < n < 1000:
        raise Bad("the quote's number is not 1 to 999")
    cur = doc.get("cur")
    if not isinstance(cur, str) or not _CUR.match(cur):
        raise Bad("the currency is not three capital letters")
    how = doc.get("how")
    if how not in ("draft", "hand"):
        raise Bad("how it was sent is not draft or hand")
    lines = doc.get("lines")
    if not isinstance(lines, list) or not lines:
        raise Bad("the quote has no lines")
    if len(lines) > LINES_MAX:
        raise Bad("{} lines, more than {}".format(len(lines), LINES_MAX))
    out_lines = []
    for k, line in enumerate(lines, start=1):
        what = "line {}".format(k)
        if not isinstance(line, dict):
            raise Bad("{} is not readable".format(what))
        out_lines.append({"seq": k, "charge": _cut(line.get("charge"), "charge"), "qty": _cut(line.get("qty"), "qty"),
                          "unit": _cut(line.get("unit"), "unit"), "rate": _number(line.get("rate"), what + " rate"),
                          "word": _cut(line.get("word"), "word"),
                          "amount": _number(line.get("amount"), what + " amount"),
                          "hand": 1 if line.get("hand") is True else 0, "remark": _cut(line.get("remark"), "remark"),
                          "cost": None, "markup": None})
    total = _number(doc.get("total"), "the total")
    if all(line["amount"] is None for line in out_lines):
        total = None
    terms = doc.get("terms")
    if terms is not None and not isinstance(terms, list):
        raise Bad("the terms are not a list")
    terms = [t for t in (_cut(x, "term") for x in (terms or [])[:TERMS_MAX]) if t]
    values = {
        "n": n, "date": _day(doc.get("date"), "the day it was sent"), "how": how, "cur": cur, "total": total,
        "at_cost": 1 if doc.get("atCost") is True else 0, "valid": _day(doc.get("valid"), "valid until", False),
        "transit": _cut(doc.get("transit"), "transit"), "routing": _cut(doc.get("routing"), "routing"),
        "remarks": _cut(doc.get("remarks"), "remarks"), "terms": json.dumps(terms, ensure_ascii=False),
        "subject": _cut(doc.get("subject"), "subject"), "pdf": _cut(doc.get("pdf"), "pdf"),
        "rate_from": _cut(doc.get("rateFrom"), "rateFrom") or "",
        "sources": json.dumps(_sources(doc.get("sources")), ensure_ascii=False),
        "by_email": _cut(doc.get("by"), "by"), "by_name": _cut(doc.get("name"), "by"), "at": iso(doc.get("at")) or None,
        "with_costs": 0, "pct": None, "cost": None, "profit": None, "margin": None, "costs_by": None,
        "costs_at": None}
    if costs is not None:
        if not isinstance(costs, dict):
            raise Bad("quoteCosts/{id} is not readable")
        if str(costs.get("opp") or "").strip().upper() != opp:
            raise Bad("the costs are for another case ({})".format(costs.get("opp") or "none"))
        if not isinstance(costs.get("cur"), str) or not _CUR.match(costs["cur"]):
            raise Bad("the costs' currency is not three capital letters")
        cost_lines = costs.get("lines")
        if not isinstance(cost_lines, list) or len(cost_lines) != len(out_lines):
            raise Bad("the costs have {} lines, the quote {}".format(
                len(cost_lines) if isinstance(cost_lines, list) else "no", len(out_lines)))
        for line, c in zip(out_lines, cost_lines):
            what = "line {}".format(line["seq"])
            if c is None:
                continue
            if not isinstance(c, dict):
                raise Bad("the costs' {} is not readable".format(what))
            line["cost"] = _number(c.get("cost"), "the costs' {} cost".format(what))
            line["markup"] = _number(c.get("markup"), "the costs' {} markup".format(what))
        values.update(with_costs=1, pct=_number(costs.get("pct"), "the markup"),
                      cost=_number(costs.get("cost"), "the cost"), profit=_number(costs.get("profit"), "the profit"),
                      margin=_number(costs.get("margin"), "the margin"), costs_by=_cut(costs.get("by"), "by"),
                      costs_at=iso(costs.get("at")) or None)
    values["lines"] = out_lines
    return values


# ------------------------------------------------------------ take in --
def waiting(conn) -> list:
    """(event, case, opp, site id) for each live event with a mark whose
    quote is not in OppList yet, oldest event first."""
    if not db._has_table(conn, "quote"):
        return []
    have = {r[0] for r in conn.execute("SELECT site_id FROM quote")}
    out, seen = [], set()
    for r in conn.execute("SELECT e.id, e.enquiry_id, e.note, n.opp_no FROM status_event e "
                          "JOIN enquiry n ON n.id = e.enquiry_id "
                          "WHERE e.voided = 0 AND e.note LIKE '%[quote:%' ORDER BY e.at, e.id"):
        for site_id in MARK.findall(r["note"] or ""):
            if site_id in have or site_id in seen:
                continue
            seen.add(site_id)
            out.append((r["id"], r["enquiry_id"], r["opp_no"], site_id))
    return out


def take_quotes(conn, client, state: dict, today: str) -> tuple:
    """Read and write the quotes waiting (at most RUN_MAX). Returns (taken,
    not taken). Never raises for a document; the caller catches the rest."""
    told = state.setdefault(STATE_KEY, {})
    log = logs.get("firebase")
    taken = failed = 0
    for event_id, case_id, opp, site_id in waiting(conn)[:RUN_MAX]:
        try:
            try:
                doc = client.get("quotes/" + site_id)
                costs = client.get("quoteCosts/" + site_id) if doc else None
            except Exception as exc:
                raise Bad("the site could not be read ({}: {})".format(type(exc).__name__, exc)) from None
            if not doc:
                raise Bad("quotes/{} is not there".format(site_id))
            values = check(doc, costs, opp)
        except Bad as exc:
            failed += 1
            if told.get(site_id) != today:
                told[site_id] = today
                log.warning("team site: the quote %s of %s was not taken in (%s); it is tried again at the next "
                            "run", site_id, opp, exc)
            continue
        repo = db.Repository(conn, source="web")
        try:
            lines = values.pop("lines")
            quote_id = repo.insert("quote", dict(values, enquiry_id=case_id, site_id=site_id,
                                                 status_event_id=event_id,
                                                 taken_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            for line in lines:
                repo.insert("quote_line", dict(line, quote_id=quote_id))
            repo.commit()
        except Exception:
            conn.rollback()
            raise
        told.pop(site_id, None)
        taken += 1
    return taken, failed


# --------------------------------------------------------- the case page --
def _money(value) -> str:
    return "" if value is None else "{:,.2f}".format(value)


def sources_text(sources, rate_from="") -> str:
    """'Freight: Harrow Lines (MSC) · Origin: Harrow Lines'; own is 'Our own
    rate', a typed name as typed."""
    parts = []
    for s in sources or []:
        name = "Our own rate" if s.get("key") == "own" else (s.get("name") or s.get("key") or "")
        if not name:
            continue
        if s.get("label") and s.get("key") != "own":
            name = "{}, {}".format(name, s["label"])
        if s.get("carrier"):
            name = "{} ({})".format(name, s["carrier"])
        parts.append("{}: {}".format(s.get("seg") or "Other", name) if s.get("seg") else name)
    if not parts and rate_from:
        parts.append("Our own rate" if rate_from == "own" else rate_from)
    return " · ".join(parts)


def _loads(text, default):
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def view(conn, enquiry_id) -> list:
    """The Quotes card's rows, newest first; the newest is open."""
    if not db._has_table(conn, "quote"):
        return []
    rows = conn.execute("SELECT q.*, e.kind AS ev_kind, e.voided AS ev_voided FROM quote q "
                        "LEFT JOIN status_event e ON e.id = q.status_event_id "
                        "WHERE q.enquiry_id = ? ORDER BY q.n DESC, q.id DESC", (enquiry_id,)).fetchall()
    out = []
    for i, q in enumerate(rows):
        lines = conn.execute("SELECT * FROM quote_line WHERE quote_id = ? ORDER BY seq", (q["id"],)).fetchall()
        terms = _loads(q["terms"], [])
        total = "{} {}".format(q["cur"], _money(q["total"])) if q["total"] is not None else ""
        if q["at_cost"]:
            total = "{} plus AT COST".format(total) if total else "{} AT COST".format(q["cur"])
        detail = ["{} line{}".format(len(lines), "" if len(lines) == 1 else "s")]
        if q["transit"]:
            detail.append("transit " + q["transit"])
        if q["routing"]:
            detail.append("routing " + q["routing"])
        if terms:
            detail.append("{} term{}".format(len(terms), "" if len(terms) == 1 else "s"))
        amounts = [line["amount"] for line in lines if line["amount"] is not None]
        costs = [line["cost"] for line in lines if line["cost"] is not None]
        with_costs = bool(q["with_costs"])
        out.append({
            "id": q["id"], "n": q["n"], "anchor": "quote-{}".format(q["n"]), "sent": q["date"],
            "requote": q["ev_kind"] == "requote", "struck": bool(q["ev_voided"]), "open": i == 0,
            "by": q["by_name"] or q["by_email"] or "", "total": total, "valid": q["valid"],
            "rates_from": sources_text(_loads(q["sources"], []), q["rate_from"] or ""),
            "with_costs": with_costs,
            "cost": _money(q["cost"]) if with_costs else "", "profit": _money(q["profit"]) if with_costs else "",
            "margin": "{:.1f}%".format(q["margin"]) if with_costs and q["margin"] is not None else "",
            "detail": " · ".join(detail), "remarks": q["remarks"] or "", "terms": terms,
            "lines": [{"charge": line["charge"] or "", "qty": line["qty"] or "", "unit": line["unit"] or "",
                       "rate": _money(line["rate"]),
                       "amount": _money(line["amount"]) if line["amount"] is not None else (line["word"] or ""),
                       "cost": _money(line["cost"]), "markup": _money(line["markup"]),
                       "remark": line["remark"] or ""} for line in lines],
            "sum_amount": _money(sum(amounts)) if amounts else "",
            "sum_cost": _money(sum(costs)) if with_costs and costs else "",
        })
    return out


def marks(conn, enquiry_id) -> dict:
    """{site id: n} of the case's quotes in OppList, for the timeline."""
    if not db._has_table(conn, "quote"):
        return {}
    return {r["site_id"]: r["n"] for r in conn.execute("SELECT site_id, n FROM quote WHERE enquiry_id = ?",
                                                        (enquiry_id,))}


def hide_marks(text) -> str:
    return _ANY_MARK.sub("", text or "").strip()


def note_html(note, known) -> Markup:
    """An event's note for the timeline: [mid:] hidden as before, each
    [quote:<id>] a link 'Quote n' to its row when the quote is in OppList,
    hidden when it is not. The note itself is stored as it was."""
    text = parsing.hide_mid(note or "")
    out, pos = [], 0
    for m in _ANY_MARK.finditer(text):
        out.append(str(escape(text[pos:m.start()])))
        site_id = m.group(0).strip()[len("[quote:"):-1]
        n = (known or {}).get(site_id)
        if n:
            out.append(' <a class="quote-link" href="#quote-{0}">Quote {0}</a>'.format(int(n)))
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return Markup("".join(out).strip())


# ---------------------------------------------------------- the Won form --
def _part(sources, rate_from) -> tuple:
    """(key, name, seg) Rate used starts from: the Freight part, else
    rate_from, else the first part with a key."""
    freight = next((s for s in sources if s.get("seg") == "Freight"), None)
    if freight is not None:
        return freight.get("key") or "", freight.get("name") or "", "Freight"
    if rate_from:
        found = next((s for s in sources if s.get("key") == rate_from), None)
        return rate_from, (found or {}).get("name") or rate_from, (found or {}).get("seg") or ""
    keyed = next((s for s in sources if s.get("key")), None)
    if keyed is not None:
        return keyed["key"], keyed.get("name") or keyed["key"], keyed.get("seg") or ""
    return "", "", ""


def won_start(conn, enquiry_id) -> dict | None:
    """The Won form's starting Rate used from the last quote not struck
    out, when no rate request of the case is awarded: {rate_used,
    rate_party, rate_request, rate_hint}; None leaves the form as today."""
    if not db._has_table(conn, "quote"):
        return None
    if conn.execute("SELECT 1 FROM vendor_request WHERE enquiry_id = ? AND voided = 0 AND awarded = 1",
                    (enquiry_id,)).fetchone():
        return None
    q = conn.execute("SELECT q.* FROM quote q JOIN status_event e ON e.id = q.status_event_id "
                     "WHERE q.enquiry_id = ? AND e.voided = 0 ORDER BY q.n DESC, q.id DESC LIMIT 1",
                     (enquiry_id,)).fetchone()
    if q is None:
        return None
    key, name, seg = _part(_loads(q["sources"], []), q["rate_from"] or "")
    if not key:
        return None                                   # a typed name or nothing: as today
    if key == "own":
        name = "Our own rate"
    hint = "from Quote {} ({})".format(q["n"], "{}: {}".format(seg, name) if seg else name)
    if key == "own":
        return {"rate_used": "own", "rate_party": None, "rate_request": None, "rate_hint": hint}
    found = conn.execute("SELECT id FROM party WHERE lower(trim(key)) = lower(?) ORDER BY id",
                         (" ".join(key.split()),)).fetchone()
    if found is None:
        return None
    from .firebase_pull import _kept
    party = _kept(conn, found["id"])
    if party is None:
        return None
    # RG2a (item 12): the party's type first, its rate request after - a
    # own office is Office rate and a carrier Carrier direct
    # even when the case asked them (Q3 read the brief's order literally)
    ptype = conn.execute("SELECT type FROM party WHERE id = ?", (party["id"],)).fetchone()["type"]
    if ptype == "Office":
        return {"rate_used": "office", "rate_party": party["id"], "rate_request": None, "rate_hint": hint}
    if ptype in db.CARRIER_TYPES:
        return {"rate_used": "carrier", "rate_party": party["id"], "rate_request": None, "rate_hint": hint}
    request = conn.execute("SELECT id FROM vendor_request WHERE enquiry_id = ? AND vendor_party_id = ? AND voided = 0 "
                           "ORDER BY asked_on DESC, id DESC LIMIT 1", (enquiry_id, party["id"])).fetchone()
    if request is not None:
        return {"rate_used": "agent", "rate_party": None, "rate_request": request["id"], "rate_hint": hint}
    return None
