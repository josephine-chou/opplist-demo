"""The demo's last stage: the features that came after the first demo world,
put where the pages show them.

The cases themselves are made by demo.py. This stage works on a few open
ones, and it does so the way the work happens: what people do on the team
site is written as the site writes it (a `rates` or `quote` action with its
vendorRates, quotes and quoteCosts documents) and taken in by OppList's own
take-in (firebase_pull.take_in, site_quotes.check), and the reading of a
vendor's reply goes through read_quote.read on a recorded answer. The
documents the site wrote are returned, so that the site's snapshot carries
them too (site_snapshot_extras): the two halves show the same work.

What it adds:
- two vendors' rates on one New case, one of them with two options;
- two quotes on that case made with the quote card, the second a re-quote,
  both with their costs;
- a reading of a third reply by Read quote, waiting to be checked;
- Waiting for requester on another New case;
- a second route (another destination) on a third New case;
- HS codes and cargo values on the cargo lines of open cases.

Everything is invented and depends on the seed and the date only. The
clock is the demo repository's: no value here reads the computer's clock.
"""

from __future__ import annotations

import datetime as dt
import random

from . import branding, db

APP = "demo"
#: the container types of a case as the quote card and the rates drawer write them
UNIT_OF = {"20GP": "20'", "40GP": "40'", "40HQ": "40'HQ", "45HQ": "45'HQ", "40RF": "40'HQ"}
#: an HS heading for each commodity the demo uses that a cargo line may carry (public classification)
HS = {"auto parts": "8708.99", "industrial pumps": "8413.70", "machinery spare parts": "8487.90",
      "copper fittings": "7412.20", "cosmetics": "3304.99", "textile yarn": "5509.53", "packaging film": "3920.10",
      "mining drill bits": "8207.19", "LED lighting": "9405.42", "printing ink": "3215.19",
      "agricultural sprayers": "8424.41", "furniture": "9403.60", "coffee": "0901.21", "wine": "2204.21",
      "frozen salmon": "0303.13", "medical devices": "9018.90", "solar panels": "8541.43", "tiles": "6907.21"}
READ_TEXT = ("Hello Iris,\n\nOur offer for the 40'HQ:\nOcean freight USD 1,840 per 40'HQ\n"
             "THC at origin USD 210 per container\nDoc fee USD 65 per BL\n"
             "Valid until 30 June 2027. Transit about 19 days, direct, with Seabright Line.\n"
             "Free time 10 days at destination.\n\nRegards")


class _Docs:
    """The site's documents as OppList's take-in reads them (client.get)."""

    def __init__(self):
        self.docs = {}

    def get(self, path):
        return self.docs.get(path)


def _bd_fwd(day: dt.date, n: int) -> dt.date:
    while n > 0:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def _at(day: dt.date, minutes: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(minutes // 60, minutes % 60))


def _person(name: str) -> dict:
    email = "{}@{}".format(name.lower().replace(" ", "."), branding.INTERNAL_DOMAIN)
    return {"by": email, "name": name, "owner": name.split()[0]}


def _open_cases(conn, status="New"):
    return conn.execute(
        "SELECT e.*, p.key AS requester FROM enquiry e LEFT JOIN party p ON p.id = e.requester_party_id "
        "WHERE e.status = ? AND (e.legacy IS NULL OR e.legacy = 0) ORDER BY e.received_on, e.opp_no",
        (status,)).fetchall()


def _vendors_of(conn, case_id):
    return [r["key"] for r in conn.execute(
        "SELECT p.key FROM vendor_request r JOIN party p ON p.id = r.vendor_party_id WHERE r.enquiry_id = ? "
        "ORDER BY r.id", (case_id,))]


def _take(repo, action_id, action, today, client=None):
    from . import firebase_pull
    ok, message = firebase_pull.take_in(repo, action_id, action, today.isoformat(), client=client)
    if not ok:
        raise RuntimeError("the demo's {} on {} was refused: {}".format(action.get("type"), action.get("opp"), message))
    return message


# ---------------------------------------------------------------- rates and quotes --
def _rate_lines(case, freight, unit, extra):
    qty = case["container_qty"] or 1
    return [{"seg": "Freight", "charge": "OCEAN FREIGHT", "unit": unit, "rate": freight, "min": None, "qty": qty,
             "amount": freight * qty, "word": None},
            {"seg": "Origin", "charge": "TERMINAL HANDLING", "unit": "CNTR", "rate": extra, "min": None, "qty": qty,
             "amount": extra * qty, "word": None},
            {"seg": "Origin", "charge": "DOCUMENTATION FEE", "unit": "BL", "rate": 75, "min": None, "qty": 1,
             "amount": 75, "word": None}]


def _rates_doc(case, vendor, label, lines, on, at, who, carrier, transit, valid, free):
    total = sum(line["amount"] for line in lines if line["amount"] is not None)
    doc = {"opp": case["opp_no"], "vendor": vendor, "cur": "USD", "total": total, "lines": lines, "valid": valid,
           "transit": transit, "carrier": carrier, "free": free,
           "said": {"fuel": "incl", "local": "excl", "min": "notsaid"}, "note": "", "route": 1, "on": on.isoformat(),
           "at": at, "by": who["by"], "name": who["name"]}
    if label is not None:
        doc["label"] = label
    return doc


def _quote_docs(case, n, day, lines, pct, who, sources, valid, transit, terms, subject):
    """quotes/{id} and quoteCosts/{id} as the quote card writes them: each line's
    cost, its markup and the sell rate; the totals, the profit and the margin."""
    sold, costs = [], []
    total = cost_total = 0
    for charge, qty, unit, cost in lines:
        markup = round(cost * pct / 100)
        rate = cost + markup
        amount = rate * qty
        total += amount
        cost_total += cost * qty
        sold.append({"charge": charge, "qty": str(qty), "unit": unit, "rate": rate, "word": "", "amount": amount,
                     "hand": False, "remark": ""})
        costs.append({"cost": cost, "markup": markup})
    profit = total - cost_total
    quote = {"opp": case["opp_no"], "n": n, "date": day.isoformat(), "cur": "USD", "how": "draft",
             "pdf": "{}_Quotation_{}.pdf".format(case["opp_no"], n), "subject": subject,
             "rateFrom": sources[0]["name"] if sources else "", "sources": sources, "lines": sold, "total": total,
             "atCost": False, "valid": valid, "transit": transit, "routing": "", "remarks": "", "terms": terms,
             "by": who["by"], "name": who["name"]}
    cost = {"opp": case["opp_no"], "cur": "USD", "pct": pct, "lines": costs, "cost": cost_total, "profit": profit,
            "margin": round(profit * 100.0 / total, 2) if total else 0, "by": who["by"]}
    return quote, cost


def rates_and_quotes(repo, today, rng) -> dict:
    """Two vendors' rates (one with two options) and two quotes on the oldest
    New FCL case with two vendors asked. Returns the site's documents."""
    from . import site_quotes
    conn = repo.conn
    case = next((c for c in _open_cases(conn) if c["service_mode"] == "FCL" and len(_vendors_of(conn, c["id"])) >= 2
                 and c["requester"] and (today - dt.date.fromisoformat(c["received_on"])).days >= 3), None)
    if case is None:
        return {}
    received = dt.date.fromisoformat(case["received_on"])
    v1, v2 = _vendors_of(conn, case["id"])[:2]
    unit = UNIT_OF.get(case["container_type"] or "", "CNTR")
    iris, victor = _person("Iris Chen"), _person("Victor Lang")
    day1, day2 = _bd_fwd(received, 1), min(_bd_fwd(received, 2), today)
    valid = (today + dt.timedelta(days=30)).isoformat()
    client, out = _Docs(), {}
    base = rng.randrange(1500, 2600, 25)
    sets = [(v1, "Direct", _rate_lines(case, base, unit, 260), "Seabright Line", "21 days", "14 days at destination", 600),
            (v1, "Via transshipment", _rate_lines(case, base - 175, unit, 260), "Orca Line", "29 days",
             "10 days at destination", 610),
            (v2, None, _rate_lines(case, base + 90, unit, 240), "Orca Line", "24 days", "7 days at destination", 640)]
    for k, (vendor, label, lines, carrier, transit, free, minute) in enumerate(sets, start=1):
        action_id = "demo-rates-{}".format(k)
        at = _at(day1, minute)
        doc = _rates_doc(case, vendor, label, lines, day1, at, iris, carrier, transit, valid, free)
        client.docs["vendorRates/" + action_id] = doc
        action = dict(iris, type="rates", opp=case["opp_no"], app=APP, at=at, vendor=vendor, cur="USD",
                      total=doc["total"], valid=valid, **({"label": label} if label is not None else {}))
        _take(repo, action_id, action, today, client)
        out["vendorRates/" + action_id] = doc
        out["actions/" + action_id] = action
    # the two quotes: the first on the direct service, the re-quote on the transshipment option
    terms = ["Rates are in USD; no other currency applies.",
             "Space and equipment are confirmed at booking, not by this quotation."]
    subject = "RE: [{}] {}".format(case["opp_no"], case["source_subject"] or "")
    qty = case["container_qty"] or 1
    for n, (day, freight, src, minute) in enumerate(((day1, base, (v1, "Direct"), 15 * 60 + 10),
                                                    (day2, base - 175, (v1, "Via transshipment"), 11 * 60 + 40)), start=1):
        quote_id = "demo-quote-{}".format(n)
        sources = [{"seg": "Freight", "key": src[0], "name": src[0], "carrier": "", "label": src[1]}]
        quote, cost = _quote_docs(case, n, day, [("OCEAN FREIGHT", qty, unit, freight), ("TERMINAL HANDLING", qty, "CNTR", 260),
                                                 ("DOCUMENTATION FEE", 1, "BL", 75), ("FORWARDING FEE", 1, "BL", 85)],
                                  10, victor, sources, valid, "21 days" if n == 1 else "29 days", terms, subject)
        at = _at(day, minute)
        quote["at"] = cost["at"] = at
        what = "Quote {} by the quote card: OCEAN FREIGHT, TERMINAL HANDLING, DOCUMENTATION FEE, FORWARDING FEE. " \
               "USD {:,.2f}. Valid until {}.".format(n, quote["total"], valid)
        action = dict(victor, type="quote", opp=case["opp_no"], app=APP, at=at, date=day.isoformat(), what=what,
                      sentBy=victor["owner"], quoteId=quote_id, rateFrom=src[0], sources=sources)
        repo.at(at)
        _take(repo, quote_id, action, today)
        client.docs["quotes/" + quote_id], client.docs["quoteCosts/" + quote_id] = quote, cost
        out["quotes/" + quote_id], out["quoteCosts/" + quote_id], out["actions/" + quote_id] = quote, cost, action
    # OppList takes the quotes in at its next run: the same check, the rows written by the demo's clock
    for event_id, case_id, opp, site_id in site_quotes.waiting(conn):
        values = site_quotes.check(client.get("quotes/" + site_id), client.get("quoteCosts/" + site_id), opp)
        lines = values.pop("lines")
        quote_row = repo.insert("quote", dict(values, enquiry_id=case_id, site_id=site_id, status_event_id=event_id,
                                              taken_at=repo.stamp))
        for line in lines:
            repo.insert("quote_line", dict(line, quote_id=quote_row))
    return out


# ---------------------------------------------------------------- the rest --
def reading(repo, today) -> bool:
    """A vendor's reply read by Read quote on the recorded answer, waiting to be
    checked: the newest open container case's first vendor."""
    from . import read_quote
    conn = repo.conn
    row = conn.execute(
        "SELECT r.id FROM vendor_request r JOIN enquiry e ON e.id = r.enquiry_id "
        "WHERE e.status IN ('New', 'Quoted') AND e.container_type IS NOT NULL ORDER BY e.id DESC, r.id LIMIT 1").fetchone()
    if row is None or not read_quote.recorded(conn):
        return False
    moment = _at(today, 9 * 60 + 5)
    repo.at(moment)
    kept = read_quote._now
    read_quote._now = lambda: moment
    try:
        day = today.isoformat()
        got = read_quote.read(conn, row["id"], read_quote.from_paste(READ_TEXT, day, day), today=day, repo=repo)
    finally:
        read_quote._now = kept
    return getattr(got, "status", "") == "read"


def waiting_for_requester(repo, today, taken) -> str | None:
    """Waiting for requester on a New case nobody handles that has no vendor asked yet."""
    conn = repo.conn
    for case in _open_cases(conn):
        if case["opp_no"] in taken or (case["handled_by"] or "").strip() or _vendors_of(conn, case["id"]) \
                or not case["requester"] or case["requester"].startswith("("):
            continue
        received = dt.date.fromisoformat(case["received_on"])
        if (today - received).days < 1:
            continue
        day = _bd_fwd(received, 1) if _bd_fwd(received, 1) <= today else today
        action = dict(_person("Iris Chen"), type="waitreq", opp=case["opp_no"], app=APP, at=_at(day, 10 * 60 + 20),
                      date=day.isoformat(), what="the weight and dimensions of each piece; the pickup address")
        repo.at(action["at"])
        _take(repo, "demo-waitreq-1", action, today)
        return case["opp_no"]
    return None


def second_route(repo, today, taken) -> str | None:
    """A second destination on a New ocean case: the requester asked to price
    another port of discharge as well (a case note says so)."""
    conn = repo.conn
    for case in _open_cases(conn):
        if case["opp_no"] in taken or case["service_mode"] not in ("FCL", "LCL"):
            continue
        route = conn.execute("SELECT * FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 ORDER BY seq",
                             (case["id"],)).fetchone()
        # another seaport of the same country
        other = conn.execute(
            "SELECT * FROM master_location WHERE type = 'seaport' AND country IN "
            "(SELECT country FROM master_location WHERE id = ?) ORDER BY name", (route["dest_loc_id"] or 0,)).fetchall()
        other = [o for o in other if o["id"] != route["dest_loc_id"]]
        if not other:
            continue
        place = other[0]
        received = dt.date.fromisoformat(case["received_on"])
        repo.at(_at(min(_bd_fwd(received, 1), today), 9 * 60 + 40))
        values = {k: route[k] for k in ("origin_country", "origin_loc", "origin_loc_id", "origin_alt", "service_mode",
                                        "incoterm", "service_scope") if k in route.keys()}
        values.update(dest_country=route["dest_country"], dest_loc=place["key"], dest_loc_id=place["id"])
        repo.add_route(case["id"], values)
        repo.insert("case_note", {"enquiry_id": case["id"],
                                  "body": "They want a price to {} as well.".format(place["name"])})
        return case["opp_no"]
    return None


def sea_air_cargo(repo) -> str | None:
    """The Sea-Air request's pallets as a cargo line, so its chargeable weight
    shows (Sea-Air is charged on air weight: it flies its last leg)."""
    conn = repo.conn
    case = conn.execute("SELECT * FROM enquiry WHERE service_mode = 'Sea-Air' ORDER BY id LIMIT 1").fetchone()
    if case is None or conn.execute("SELECT 1 FROM enquiry_cargo WHERE enquiry_id = ? AND voided = 0",
                                    (case["id"],)).fetchone():
        return None
    repo.add_cargo_line(case["id"], {
        "label": "1", "pieces": 6, "pack_word": "pallets", "dims_text": "120 x 100 x 76 cm", "length_cm": 120,
        "width_cm": 100, "height_cm": 76, "weight_kg": 1200, "commodity": case["commodity"],
        "hs_code": HS.get(case["commodity"] or ""), "cargo_value": 38500, "cargo_value_cur": "USD", "source": "mail"})
    return case["opp_no"]


def cargo_details(repo, rng, opps) -> int:
    """Cargo lines with an HS code and a value on open container cases (the
    cases of this stage first): one line per case, its containers, the
    commodity of the case. A case that has cargo lines already is left alone."""
    conn = repo.conn
    done = 0
    cases = conn.execute(
        "SELECT * FROM enquiry WHERE status IN ('New', 'Quoted') AND service_mode = 'FCL' AND container_type IS NOT NULL "
        "AND commodity IS NOT NULL ORDER BY opp_no").fetchall()
    cases = sorted(cases, key=lambda c: (c["opp_no"] not in opps, c["opp_no"]))
    for case in cases:
        code = HS.get(case["commodity"])
        if not code or conn.execute("SELECT 1 FROM enquiry_cargo WHERE enquiry_id = ? AND voided = 0",
                                    (case["id"],)).fetchone():
            continue
        repo.add_cargo_line(case["id"], {
            "label": "1", "container_type": case["container_type"], "container_qty": case["container_qty"] or 1,
            "commodity": case["commodity"], "hs_code": code, "cargo_value": rng.randrange(18000, 140000, 500),
            "cargo_value_cur": "USD", "source": "manual"})
        done += 1
        if done == 4:
            break
    return done


def run(ex) -> dict:
    """The stage; returns the site's documents {path: fields}."""
    world, repo = ex.world, ex.repo
    today = world.today
    rng = random.Random(world.seed * 7919 + 101)
    docs = rates_and_quotes(repo, today, rng)
    taken = {d["opp"] for d in docs.values() if isinstance(d, dict) and d.get("opp")}
    reading(repo, today)
    wait = waiting_for_requester(repo, today, taken)
    taken.add(wait)
    taken.add(second_route(repo, today, taken))
    taken.add(sea_air_cargo(repo))
    cargo_details(repo, rng, taken)
    return docs
