"""Batch Q2b (2026-11-13, schema v52): vendor kinds Coloader and Trucker; the
part and the route of a rate request; the route and the day of a vendor's
rates; every carrier, coloader and trucker on Ask vendors' list
(INTERFACE.md sections 1, 5 and 9).

Every company, person, place, amount and date here is invented; cases from
OPP-9875, domains .test, invented vendor names.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, firebase_pull, firebase_sync as fs, firebase_vendors, schema_migrations, site_rates
from opplist import web as web_module
from opplist.web import create_app

TODAY = "2027-03-03"
AT = dt.datetime(2027, 3, 2, 15, 0, tzinfo=dt.timezone.utc)
LINES = [{"seg": "Freight", "charge": "Ocean freight", "unit": "40HQ", "rate": 1800, "min": None, "qty": 1,
          "amount": 1800, "word": None}]


class FakeClient:
    def __init__(self):
        self.docs = {}

    def get(self, path):
        return self.docs.get(path)


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "q2b.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    agent = add_party(repo, "Tolvane Freight", country="Chile")
    ferrow = add_party(repo, "Ferrow Lines", ptype="Carrier", country="USA")
    pallis = add_party(repo, "Pallis Consol", ptype="Coloader", country="USA")
    ridge = add_party(repo, "Ridgeway Haulage", ptype="Trucker", country="USA")
    ids = {"path": path, "ferrow": ferrow, "pallis": pallis, "ridge": ridge}
    ids[9875] = add_enquiry(repo, "OPP-9875", agent, status="New", received="2027-02-27", scope="In",
                            service_mode="FCL", origin_country="USA", origin_loc="MIA", dest_country="Chile",
                            dest_loc="SAI")
    ids["A"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids[9875],)).fetchone()[0]
    ids["B"] = repo.add_route(ids[9875], {"origin_country": "USA", "origin_loc": "LAX", "dest_country": "Chile",
                                          "dest_loc": "SAI", "service_mode": "FCL"})
    ids[9876] = add_enquiry(repo, "OPP-9876", agent, status="New", received="2027-02-27", scope="In",
                            service_mode="LCL", origin_country="USA", dest_country="Chile")
    ids["one"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids[9876],)).fetchone()[0]
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    yield {"conn": conn, "ids": ids, "path": path, "client": FakeClient()}
    conn.close()


def ask(w, action_id, **fields):
    repo = db.Repository(w["conn"], source="web")
    action = {"type": "ask", "opp": "OPP-9875", "by": "ava@jojofreight.example", "name": "Ava", "owner": "Ava", "app": "site",
              "at": AT, "date": "2027-03-02", "subject": "[OPP-9875] RATE REQUEST // MIAMI - SAN ANTONIO"}
    action.update(fields)
    ok, message = firebase_pull.take_in(repo, action_id, action, TODAY)
    repo.commit()
    return ok, message


def rates(w, action_id, opp="OPP-9875", vendor="Ferrow Lines", total=2000, **extra):
    doc = {"opp": opp, "vendor": vendor, "cur": "USD", "total": total, "lines": LINES, "valid": "2027-04-30",
           "said": {"fuel": "incl", "local": "excl", "min": "none"}, "by": "ava@jojofreight.example", "name": "Ava", "at": AT}
    doc.update(extra)
    w["client"].docs["vendorRates/" + action_id] = doc
    repo = db.Repository(w["conn"], source="web")
    action = {"type": "rates", "opp": opp, "by": "ava@jojofreight.example", "name": "Ava", "owner": "Ava", "app": "site",
              "at": AT, "vendor": vendor, "cur": "USD", "total": total, "valid": "2027-04-30"}
    action.update({k: v for k, v in extra.items() if k in ("route", "on")})
    ok, message = firebase_pull.take_in(repo, action_id, action, TODAY, client=w["client"])
    repo.commit()
    return ok, message


def requests_of(w, case=9875, party="ferrow"):
    return w["conn"].execute("SELECT * FROM vendor_request WHERE enquiry_id = ? AND vendor_party_id = ? AND voided = 0 "
                             "ORDER BY id", (w["ids"][case], w["ids"][party])).fetchall()


# ------------------------------------------------------------- 1. v52 --
def _back_to_v51(path):
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        raw.execute("DROP VIEW {}".format(name))

    def rebuild(table, edit):
        sql = re.sub(r"--[^\n]*", "", raw.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0])
        new_sql = edit(sql).replace("CREATE TABLE {}".format(table), "CREATE TABLE {}_v51".format(table), 1)
        raw.execute(new_sql)
        columns = [r[1] for r in raw.execute("PRAGMA table_info({}_v51)".format(table))]
        raw.execute("INSERT INTO {t}_v51 ({c}) SELECT {c} FROM {t}".format(t=table, c=", ".join(columns)))
        raw.execute("DROP TABLE {}".format(table))
        raw.execute("ALTER TABLE {0}_v51 RENAME TO {0}".format(table))

    raw.execute("UPDATE party SET type = 'Carrier' WHERE type IN ('Coloader', 'Trucker')")      # as before v52
    rebuild("party", lambda s: s.replace(",'Coloader','Trucker'", ""))
    raw.execute("CREATE UNIQUE INDEX ux_party_key_ci ON party (key COLLATE NOCASE)")
    rebuild("vendor_request", lambda s: re.sub(r",\s*parts\s+TEXT", "", s))
    rebuild("vendor_rate", lambda s: re.sub(r",\s*route_id\s+INTEGER REFERENCES enquiry_route\(id\),\s*received_on\s+TEXT", "", s))
    raw.execute("UPDATE setting SET value = '51' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()


def test_v52_keeps_every_row_takes_a_coloader_and_refuses_a_type_outside_the_list(world):
    w = world
    conn = w["conn"]
    repo = db.Repository(conn, source="test")
    repo.insert("contact", {"party_id": w["ids"]["ferrow"], "name": "Dov Ferrow", "email": "dov@ferrowlines.test"})
    repo.insert("party_domain", {"party_id": w["ids"]["ferrow"], "domain": "ferrowlines.test"})
    repo.insert("vendor_rate", {"enquiry_id": w["ids"][9876], "vendor_party_id": w["ids"]["ferrow"], "site_id": "old-1",
                                "at": "2027-03-02T15:00:00.000000Z", "cur": "USD", "total": 900, "source": "web"})
    repo.commit()
    conn.close()
    _back_to_v51(w["path"])
    raw = sqlite3.connect(w["path"])
    assert "'Coloader'" not in raw.execute("SELECT sql FROM sqlite_master WHERE name = 'party'").fetchone()[0]
    raw.close()
    notes = []
    conn = db.open_db(w["path"], upgrade_notes=notes)
    try:
        assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 52
        counts = next(n for n in notes if n.startswith("rows before and after: "))
        assert "party 4," in counts and "contact 1" in counts and "party_domain 1" in counts and "vendor_rate 1" in counts
        assert "foreign_key_check: nothing" in notes and "vendor_request.parts added" in notes
        assert "vendor_rate rows filled (received_on, route_id): 1" in notes
        old = conn.execute("SELECT received_on, route_id FROM vendor_rate WHERE site_id = 'old-1'").fetchone()
        assert tuple(old) == ("2027-03-02", w["ids"]["one"])
        # no party's type is changed by the upgrade
        assert conn.execute("SELECT type FROM party WHERE id = ?", (w["ids"]["pallis"],)).fetchone()[0] == "Carrier"
        repo = db.Repository(conn, source="test")
        repo.insert("party", {"key": "Selvan Consol", "full_name": "Selvan Consol", "type": "Coloader", "country": "USA"})
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert("party", {"key": "Odd Broker", "full_name": "Odd Broker", "type": "Broker", "country": "USA"})
    finally:
        conn.close()


# ------------------------------------------------------------- 3. ask --
def test_an_ask_with_parts_and_routes_and_the_same_vendor_again(world):
    w = world
    ok, message = ask(w, "a1", vendors=["Ferrow Lines"], parts=["Inland", "Freight"], routes=[2])
    assert ok and message.startswith("OPP-9875: a rate request to Ferrow Lines logged on 2027-03-02")
    [r] = requests_of(w)
    assert (r["parts"], r["route_ids"]) == ("Freight|Inland", str(w["ids"]["B"]))
    rr = fs._rate_requests(w["conn"], {"id": w["ids"][9875]})
    assert (rr[0]["parts"], rr[0]["routes"]) == (["Freight", "Inland"], [2])
    ok, message = ask(w, "a2", vendors=["Ferrow Lines"], parts=["Origin"], routes=[1])
    assert ok and "Already on the case: Ferrow Lines, now also Origin, every route." in message
    [r] = requests_of(w)                                                       # one request, both kept
    assert (r["parts"], r["route_ids"]) == ("Freight|Inland|Origin", None)
    assert w["conn"].execute("SELECT source FROM change_log WHERE table_name = 'vendor_request' AND row_id = ? "
                             "AND field = 'parts' ORDER BY id DESC", (r["id"],)).fetchone()[0] == "web"


def test_a_part_and_a_route_not_known_are_left_out_and_named(world):
    w = world
    ok, message = ask(w, "a3", vendors=["Pallis Consol"], parts=["Freight", "Pier"], routes=[1, 7])
    assert ok and "'Pier' is not a part OppList knows" in message and "route 7 is not a route of OPP-9875" in message
    [r] = requests_of(w, party="pallis")
    assert (r["parts"], r["route_ids"]) == ("Freight", str(w["ids"]["A"]))


def test_an_ask_without_parts_and_routes_is_as_before(world):
    w = world
    ok, _ = ask(w, "a4", vendors=["Ferrow Lines"])
    [r] = requests_of(w)
    assert ok and (r["parts"], r["route_ids"]) == (None, None)


# ----------------------------------------------------------- 4. rates --
def test_rates_with_route_and_on_mark_the_request_that_covers_the_route(world):
    w = world
    ask(w, "a5", vendors=["Ferrow Lines"], parts=["Freight"], routes=[2])
    ok, message = rates(w, "r1", route=2, on="2027-03-01")
    assert ok, message
    v = w["conn"].execute("SELECT route_id, received_on FROM vendor_rate WHERE site_id = 'r1'").fetchone()
    assert tuple(v) == (w["ids"]["B"], "2027-03-01")
    [r] = requests_of(w)
    assert (r["replied_on"], r["machine_status"], r["quoted"]) == ("2027-03-01", "Quoted", 1)
    # route A: none of its requests covers it - one is made for it
    ok, _ = rates(w, "r2", route=1, total=1900)
    assert ok
    made = requests_of(w)[-1]
    assert (made["route_ids"], made["asked_on"], made["replied_on"], made["machine_status"]) == \
        (str(w["ids"]["A"]), "2027-03-02", "2027-03-02", "Quoted")


@pytest.mark.parametrize("extra, why", [({"route": 9}, "route 9 is not a route of the case"),
                                        ({"on": "2027-03-04"}, "2027-03-04 is after today"),
                                        ({"on": "03/01/2027"}, "'03/01/2027' is not a date")])
def test_a_route_not_on_the_case_and_an_on_after_today_are_refused(world, extra, why):
    ok, message = rates(world, "x-" + str(list(extra.values())[0]).replace("/", ""), **extra)
    assert not ok and why in message


def test_a_one_route_case_with_no_route_given(world):
    w = world
    ok, _ = rates(w, "r3", opp="OPP-9876", vendor="Ridgeway Haulage")
    v = w["conn"].execute("SELECT route_id, received_on FROM vendor_rate WHERE site_id = 'r3'").fetchone()
    assert ok and tuple(v) == (w["ids"]["one"], "2027-03-02")
    [made] = requests_of(w, 9876, "ridge")
    assert made["route_ids"] is None                                              # one route: every route


# ------------------------------------------------- 5. lists/vendors --
def test_lists_vendors_has_a_trucker_never_asked_and_parts(world):
    w = world
    ask(w, "a6", vendors=["Ferrow Lines"], parts=["Inland", "Freight"], routes=[2])
    rows = {r["key"]: r for r in firebase_vendors.rows(w["conn"], w["path"].parent / "none" / "mail.db")[0]}
    ridge = rows["Ridgeway Haulage"]
    assert (ridge["type"], ridge["asked"], ridge["quoted"], ridge["lastAsked"], ridge["parts"]) == ("Trucker", 0, 0, "", [])
    assert rows["Ferrow Lines"]["parts"] == ["Freight", "Inland"] and rows["Pallis Consol"]["type"] == "Coloader"
    assert "Tolvane Freight" not in rows                                         # an agent never asked is not


# ------------------------------------------------- 7. the case page --
def test_the_case_page_row_its_edit_form_and_rates_per_route(world):
    w = world
    ask(w, "a7", vendors=["Ferrow Lines", "Pallis Consol"], parts=["Inland"], routes=[2])
    rates(w, "r4", route=2, total=2100)
    rates(w, "r5", vendor="Pallis Consol", route=2, total=2300)
    app = create_app(w["path"])
    app.config["TESTING"] = True
    client = app.test_client()
    page = client.get("/enquiries/{}".format(w["ids"][9875])).data.decode("utf-8")
    card = page[page.index('id="vendors"'):]
    card = card[:card.index("</section>")]
    ferrow = card[card.index(">Ferrow Lines<"):card.index(">Pallis Consol<")]
    assert '<span class="muted small v-parts">Inland &middot; B</span>' in ferrow
    assert '<span class="chip">B</span><span class="rates-total">USD 2,100</span>' in ferrow and ">Lowest<" in ferrow
    assert 'name="parts_present"' in ferrow and 'value="Inland" form="vendor-' in ferrow
    request_id = requests_of(w)[0]["id"]
    client.post("/enquiries/{}/vendors".format(w["ids"][9875]),
                data={"vendor_request_id": request_id, "action": "save", "asked_on": "2027-03-02", "parts_present": "1",
                      "part": ["Customs", "Inland"], "routes_present": "1", "route": [str(w["ids"]["B"])]})
    assert requests_of(w)[0]["parts"] == "Inland|Customs"


def test_the_type_menus_offer_coloader_and_trucker(world):
    w = world
    app = create_app(w["path"])
    app.config["TESTING"] = True
    client = app.test_client()
    for url in ("/parties", "/parties/{}".format(w["ids"]["ferrow"]), "/enquiries/new",
                "/enquiries/{}".format(w["ids"][9875])):
        page = client.get(url).data.decode("utf-8")
        assert ">Coloader<" in page and ">Trucker<" in page, url
        assert page.index(">Carrier<") < page.index(">Coloader<") < page.index(">Trucker<"), url
    from opplist import site_close
    assert site_close.RATE_OF_TYPE["Coloader"] == site_close.RATE_OF_TYPE["Trucker"] == "carrier"
    assert set(db.CARRIER_TYPES) <= set(db.PARTY_TYPES)
