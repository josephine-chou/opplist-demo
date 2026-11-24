"""Batch Q2a (2026-11-13, schema v51): the vendors' rates on the case, typed
on the team site, every version kept in OppList and shown on the case
page's Rate requests (INTERFACE.md section 9).

Every company, person, amount and date here is invented; cases from
OPP-9810, domains .test.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_pull, site_rates
from opplist import web as web_module
from opplist.web import create_app

TODAY = "2027-03-03"
AT = dt.datetime(2027, 3, 2, 15, 0, tzinfo=dt.timezone.utc)
LINES = [{"seg": "Freight", "charge": "Ocean freight", "unit": "40HQ", "rate": 1800, "min": None, "qty": 1,
          "amount": 1800, "word": None},
         {"seg": "Origin", "charge": "Pick up", "unit": "TRIP", "rate": 550, "min": None, "qty": 1, "amount": 550,
          "word": None},
         {"seg": "Destination", "charge": "Customs clearance", "unit": "SHPT", "rate": None, "min": None, "qty": None,
          "amount": None, "word": "AT COST"}]


class FakeClient:
    def __init__(self):
        self.docs = {}

    def get(self, path):
        return self.docs.get(path)


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "q2a.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Calden Freight", country="Chile")
    orvell = add_party(repo, "Orvell Logistics", country="USA")
    pemberly = add_party(repo, "Pemberly Air", country="USA")
    ids = {"path": path, "orvell": orvell, "pemberly": pemberly}
    for n, status, scope in ((9810, "New", "In"), (9811, "Won", "In"), (9812, "New", "Out"), (9813, "Quoted", "In")):
        ids[n] = add_enquiry(repo, "OPP-{}".format(n), agent, status=status, scope=scope, received="2027-02-27",
                             service_mode="FCL", origin_country="USA", dest_country="Chile")
    ids["asked"] = repo.insert("vendor_request", {"enquiry_id": ids[9810], "vendor_party_id": orvell,
                                                  "asked_on": "2027-02-28", "source": "web"})
    repo.insert("vendor_request", {"enquiry_id": ids[9813], "vendor_party_id": pemberly, "asked_on": "2027-02-20",
                                   "source": "manual"})              # Pemberly is a listed vendor, not on 9810
    repo.insert("vendor_request", {"enquiry_id": ids[9811], "vendor_party_id": orvell, "asked_on": "2027-02-28",
                                   "source": "manual"})
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    yield {"conn": conn, "ids": ids, "path": path, "client": FakeClient()}
    conn.close()


def doc(opp="OPP-9810", vendor="Orvell Logistics", total=2350, cur="USD", valid="2027-04-30", lines=None, said=None,
        **extra):
    out = {"opp": opp, "vendor": vendor, "cur": cur, "total": total, "lines": LINES if lines is None else lines,
           "valid": valid, "transit": "18 days", "carrier": "Tarvell Line", "free": "14 days",
           "said": said or {"fuel": "incl", "local": "excl", "min": "none"}, "note": "Subject to space",
           "by": "ava@jojofreight.example", "name": "Ava", "at": AT}
    out.update(extra)
    return out


def rates(w, action_id, document, opp=None, vendor=None, put=True):
    if put:
        w["client"].docs["vendorRates/" + action_id] = document
    repo = db.Repository(w["conn"], source="web")
    action = {"type": "rates", "opp": opp or document["opp"], "by": "ava@jojofreight.example", "name": "Ava",
              "owner": "Ava", "app": "site", "at": AT, "vendor": vendor or document["vendor"],
              "cur": document.get("cur"), "total": document.get("total"), "valid": document.get("valid")}
    ok, message = firebase_pull.take_in(repo, action_id, action, TODAY, client=w["client"])
    repo.commit()
    return ok, message


def versions(w, case):
    return w["conn"].execute("SELECT * FROM vendor_rate WHERE enquiry_id = ? ORDER BY id", (w["ids"][case],)).fetchall()


# ---------------------------------------------------------- 1. take in --
def test_rates_are_kept_with_their_lines_and_the_request_marked(world):
    w = world
    ok, message = rates(w, "r1", doc())
    assert ok and message == "OPP-9810: rates from Orvell Logistics kept (USD 2,350, valid until Apr 30)."
    [row] = versions(w, 9810)
    assert (row["site_id"], row["cur"], row["total"], row["valid_until"], row["transit"], row["carrier"], row["free_time"],
            row["said_fuel"], row["said_local"], row["said_min"], row["note"], row["by_name"], row["source"]) == \
        ("r1", "USD", 2350, "2027-04-30", "18 days", "Tarvell Line", "14 days", "incl", "excl", "none",
         "Subject to space", "Ava", "web")
    lines = w["conn"].execute("SELECT seq, seg, charge, unit, rate, qty, amount, word FROM vendor_rate_line "
                              "WHERE rate_id = ? ORDER BY seq", (row["id"],)).fetchall()
    assert [tuple(l) for l in lines] == [(1, "Freight", "Ocean freight", "40HQ", 1800, 1, 1800, None),
                                         (2, "Origin", "Pick up", "TRIP", 550, 1, 550, None),
                                         (3, "Destination", "Customs clearance", "SHPT", None, None, None, "AT COST")]
    request = w["conn"].execute("SELECT * FROM vendor_request WHERE id = ?", (w["ids"]["asked"],)).fetchone()
    assert (request["replied_on"], request["machine_status"], request["quoted"]) == ("2027-03-02", "Quoted", 1)
    assert w["conn"].execute("SELECT COUNT(*) FROM change_log WHERE table_name IN ('vendor_rate', 'vendor_rate_line') "
                             "AND source = 'web'").fetchone()[0] == 4


def test_a_second_save_is_a_second_version_and_the_same_id_is_one(world):
    w = world
    rates(w, "r1", doc())
    ok, _ = rates(w, "r2", doc(total=2200, valid="2027-05-15"))
    assert ok and [r["site_id"] for r in versions(w, 9810)] == ["r1", "r2"]          # the first stays
    case = w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (w["ids"][9810],)).fetchone()
    again = site_rates.take(db.Repository(w["conn"], source="web"), case, {"vendor": "Orvell Logistics", "at": AT},
                            "Ava", TODAY, "r2", w["client"])
    assert again == "OPP-9810: rates from Orvell Logistics kept (USD 2,200, valid until May 15)."
    assert len(versions(w, 9810)) == 2                                                 # not a third
    [card] = site_rates.card(w["conn"], w["ids"][9810], TODAY)[w["ids"]["orvell"]]          # Q2b: one route, one entry
    assert (card["total"], card["older"]) == ("USD 2,200", 1)


def test_a_closed_cases_rates_are_kept(world):
    w = world
    ok, message = rates(w, "r3", doc(opp="OPP-9811", total=900))
    assert ok and len(versions(w, 9811)) == 1


def test_a_vendor_without_a_request_on_the_case_gets_one_made(world):
    w = world
    ok, message = rates(w, "r4", doc(vendor="Pemberly Air", total=2100))
    assert ok, message
    made = w["conn"].execute("SELECT * FROM vendor_request WHERE enquiry_id = ? AND vendor_party_id = ?",
                             (w["ids"][9810], w["ids"]["pemberly"])).fetchone()
    assert (made["asked_on"], made["replied_on"], made["machine_status"], made["quoted"], made["source"], made["notes"]) == \
        ("2027-03-02", "2027-03-02", "Quoted", 1, "web", "Rates typed on the team site by Ava")


@pytest.mark.parametrize("name, document, kwargs, why", [
    ("out", doc(opp="OPP-9812"), {}, "is not in the lists"),
    ("stranger", doc(vendor="Nobody Shipping"), {}, "is not a vendor in OppList"),
    ("seg", doc(lines=[dict(LINES[0], seg="Pier")]), {}, "'Pier' is not one of Freight"),
    ("many", doc(lines=[LINES[0]] * 41), {}, "41 lines, more than 40"),
    ("word", doc(lines=[dict(LINES[0], word="FREE")]), {}, "'FREE' is not one of AT COST"),
    ("said", doc(said={"fuel": "maybe", "local": "excl", "min": "none"}), {}, "Fuel: 'maybe'"),
    ("long", doc(carrier="x" * 61), {}, "longer than 60"),
    ("gone", doc(), {"put": False}, "could not be read"),
])
def test_refused_rates(world, name, document, kwargs, why):
    w = world
    ok, message = rates(w, "x-" + name, document, **kwargs)
    assert not ok and why in message, message
    assert w["conn"].execute("SELECT COUNT(*) FROM vendor_rate").fetchone()[0] == 0
    assert w["conn"].execute("SELECT result FROM site_action WHERE id = ?", ("x-" + name,)).fetchone()[0] == "refused"


# ------------------------------------------------------ 3. the case page --
def test_the_rate_requests_card_shows_each_vendors_newest_rates_and_their_lines(world):
    w = world
    rates(w, "r1", doc(total=2600))
    rates(w, "r2", doc(total=2350, valid="2027-03-01", said={"fuel": "notsaid", "local": "excl", "min": "notsaid"}))
    rates(w, "r4", doc(vendor="Pemberly Air", total=2500, valid="2027-04-30"))
    app = create_app(w["path"])
    app.config["TESTING"] = True
    page = app.test_client().get("/enquiries/{}".format(w["ids"][9810])).data.decode("utf-8")
    card = page[page.index('id="vendors"'):]
    card = card[:card.index("</section>")]
    orvell = card[card.index("Orvell Logistics"):]
    orvell = orvell[orvell.index('class="v-rates"'):orvell.index("</details>")]
    # Q2e (INTERFACE.md section 13): a past valid until is left out of Lowest, so with Orvell's
    # Mar 1 past, Pemberly is alone on the route and neither is Lowest
    assert "USD 2,350" in orvell and ">Lowest<" not in orvell
    assert 'class="rates-valid warn"' in orvell                                   # Mar 1 is past
    assert "Fuel not said" in orvell and "Minimum not said" in orvell and "Local charges not said" not in orvell
    assert "Ocean freight" in orvell and "AT COST" in orvell and "1,800" in orvell
    assert "Typed on the team site by Ava" in orvell and "1 older version" in orvell
    pemberly = card[card.index("Pemberly Air"):]
    pemberly = pemberly[pemberly.index('class="v-rates"'):pemberly.index("</details>")]
    assert "USD 2,500" in pemberly and "Lowest" not in pemberly and 'rates-valid warn' not in pemberly
