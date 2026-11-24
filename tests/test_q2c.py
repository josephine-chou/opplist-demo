"""Batch Q2c (2026-11-15, no schema change): the vendors' rates pushed to the
Rate Card's Agent quotes (INTERFACE.md section 10); a version typed by
mistake struck out; the role coloader; the site's words "logged by".

A fake client as the team site's sync tests. Every company, person, place,
amount and date here is invented; cases from OPP-9900, domains .test,
invented vendor names.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import add_enquiry, add_party
from opplist import (db, firebase_pull, firebase_sync as fs, firebase_vendors, migrate_master, ratecard_sync,
                     site_edit, site_rates)
from opplist import web as web_module
from opplist.web import create_app
from test_firebase_sync import FakeFirestore

TODAY = "2027-03-05"
FCL_LINES = [
    {"seg": "Freight", "charge": "OCEAN FREIGHT", "unit": "40HQ", "rate": 1800, "min": None, "qty": 2, "amount": 3600,
     "word": None},
    {"seg": "Inland", "charge": "DRAYAGE", "unit": "TRIP", "rate": 450, "min": None, "qty": 1, "amount": 450,
     "word": None},
    {"seg": "Customs", "charge": "DUTIES AND TAXES", "unit": "SHIPMENT", "rate": None, "min": None, "qty": None,
     "amount": None, "word": "AT COST"}]


class DocClient:
    """vendorRates/{id} as the site writes them."""

    def __init__(self):
        self.docs = {}

    def get(self, path):
        return self.docs.get(path)


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "q2c.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for name, region, iso2 in (("USA", "North America", "US"), ("Chile", "Latin America", "CL"),
                               ("Brazil", "Latin America", "BR"), ("Unknown", "Undefined", None)):
        repo.insert("country", {"name": name, "region": region, "iso2": iso2})
    miami = repo.add_master_location("MIAMI, FL", "USA", "city", name="Miami", subdivision="FL", source="phase1")
    lax = repo.add_master_location("LAX", "USA", "airport", name="Los Angeles International", iata="LAX", is_airport=1,
                                   source="phase1")
    sai = repo.add_master_location("SAN ANTONIO", "Chile", "seaport", name="San Antonio", unlocode="CLSAI",
                                   is_seaport=1, source="phase1")
    vap = repo.add_master_location("VALPARAISO", "Chile", "seaport", name="Valparaiso", unlocode="CLVAP",
                                   is_seaport=1, source="phase1")
    scl = repo.add_master_location("SCL", "Chile", "airport", name="Santiago Merino Benitez", iata="SCL",
                                   is_airport=1, source="phase1")
    conn.execute("ALTER TABLE location RENAME TO location_old")
    conn.execute(migrate_master.LOCATION_VIEW_SQL)
    agent = add_party(repo, "Corvane Logistics", country="Chile")
    ostrava = add_party(repo, "Ostrava Shipping S.A.", ptype="Carrier", country="USA")
    belden = add_party(repo, "Belden Air", ptype="Agent", country="Unknown", roles="agent|coloader")
    ids = {"path": path, "agent": agent, "ostrava": ostrava, "belden": belden}
    # one-route FCL: Miami to San Antonio, port of discharge Valparaiso
    ids[9900] = add_enquiry(repo, "OPP-9900", agent, status="New", received="2027-03-01", scope="In",
                            service_mode="FCL", origin_country="USA", dest_country="Chile", incoterm="FOB",
                            commodity="Slate tiles", special_handling="DG")
    ids["A0"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids[9900],)).fetchone()[0]
    repo.edit_route(ids["A0"], {"origin_loc_id": miami["id"], "origin_loc": "MIAMI, FL", "dest_places": [sai["id"]],
                                "pod_places": [vap["id"]], "service_scope": "Door to port", "service_mode": "FCL"})
    repo.add_cargo_line(ids[9900], {"container_qty": 2, "container_type": "40HQ", "commodity": "Slate tiles"})
    repo.add_cargo_line(ids[9900], {"container_qty": 1, "container_type": "20GP", "commodity": "Slate tiles"})
    # two routes: A LCL Miami - San Antonio, B Air LAX - SCL (no incoterm on B: the case's)
    ids[9901] = add_enquiry(repo, "OPP-9901", agent, status="Quoted", received="2027-03-01", scope="In",
                            service_mode="LCL", origin_country="USA", dest_country="Chile", incoterm="EXW",
                            commodity="Lamps")
    ids["A1"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (ids[9901],)).fetchone()[0]
    repo.edit_route(ids["A1"], {"origin_loc_id": miami["id"], "origin_loc": "MIAMI, FL", "dest_places": [sai["id"]],
                                "service_scope": "Port to port"})
    ids["B1"] = repo.add_route(ids[9901], {"origin_country": "USA", "origin_loc_id": lax["id"], "origin_loc": "LAX",
                                           "dest_country": "Chile", "dest_places": [scl["id"]], "service_mode": "Air"})
    repo.add_cargo_line(ids[9901], {"pieces": 4, "pack_unit": "Pallets", "weight_value": 500, "weight_unit": "kg",
                                    "cbm_value": 3, "commodity": "Lamps", "route_ids": str(ids["A1"])})
    repo.add_cargo_line(ids[9901], {"pieces": 2, "pack_unit": "Crates", "weight_value": 100, "weight_unit": "kg",
                                    "cbm_value": 1.2, "commodity": "Lamp shades", "route_ids": str(ids["B1"])})
    ids[9902] = add_enquiry(repo, "OPP-9902", agent, status="New", received="2027-03-01", scope="In",
                            service_mode="FCL", origin_country="USA", dest_country="Chile")
    repo.commit()
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    yield {"conn": conn, "ids": ids, "path": path, "docs": DocClient(), "state": tmp_path / "ratecard-sync.json"}
    conn.close()


def rates(w, action_id, opp, vendor, total, route=None, on=None, lines=None, at=None, said=None, **extra):
    at = at or dt.datetime(2027, 3, 2, 15, 0, tzinfo=dt.timezone.utc)
    doc = {"opp": opp, "vendor": vendor, "cur": "USD", "total": total, "lines": FCL_LINES if lines is None else lines,
           "valid": "2027-04-30", "transit": "18 days", "carrier": "Tarvell Line", "free": "14 days",
           "said": said or {"fuel": "notsaid", "local": "excl", "min": "notsaid"}, "note": "Subject to space",
           "by": "ava@jojofreight.example", "name": "Ava", "at": at, "route": route, "on": on}
    doc.update(extra)
    w["docs"].docs["vendorRates/" + action_id] = doc
    repo = db.Repository(w["conn"], source="web")
    ok, message = firebase_pull.take_in(repo, action_id, {
        "type": "rates", "opp": opp, "by": "ava@jojofreight.example", "name": "Ava", "owner": "Ava", "app": "site", "at": at,
        "vendor": vendor, "route": route, "on": on}, TODAY, client=w["docs"])
    repo.commit()
    assert ok, message
    return w["conn"].execute("SELECT id FROM vendor_rate WHERE site_id = ?", (action_id,)).fetchone()[0]


def built(w, ids=None):
    return ratecard_sync.build(w["conn"], {} if ids is None else ids)


def push(w, client):
    state = ratecard_sync.load_state(w["state"])
    result = ratecard_sync.push(w["conn"], client, state)
    ratecard_sync.save_state(state, w["state"])
    return result


# ------------------------------------------------------------ 2. the fields --
def test_every_field_on_a_one_route_fcl_case(world):
    w = world
    rates(w, "r1", "OPP-9900", "Ostrava Shipping S.A.", 4050, on="2027-03-01")
    docs = built(w)
    assert list(docs) == ["OPP-9900_A_ostrava-shipping-s-a_1"]
    doc = docs["OPP-9900_A_ostrava-shipping-s-a_1"]
    assert str(doc.pop("loggedAt")) == "2027-03-02T15:00:00.000000Z"
    assert doc == {
        "schema": 1, "opp": "OPP-9900", "route": "A", "seq": 1, "rev": 1, "receivedOn": "2027-03-01",
        "loggedBy": "Ava", "how": "web", "confirmedBy": "", "mode": "FCL",
        "origin": {"key": "MIAMI, FL", "name": "Miami", "country": "USA", "unlocode": "", "iata": ""},
        "destination": {"key": "SAN ANTONIO", "name": "San Antonio", "country": "Chile", "unlocode": "CLSAI", "iata": ""},
        "pod": {"key": "VALPARAISO", "name": "Valparaiso", "country": "Chile", "unlocode": "CLVAP", "iata": ""},
        "scope": "D2P", "incoterm": "FOB",
        "cargo": {"containers": [{"eq": "40'HQ", "qty": 2}, {"eq": "20'", "qty": 1}], "commodity": "Slate tiles",
                  "dg": True},
        "vendor": {"key": "Ostrava Shipping S.A.", "name": "Ostrava Shipping S.A.", "country": "USA", "kind": "carrier"},
        "carrier": "Tarvell Line", "transit": "18 days", "freeTime": "14 days", "validUntil": "2027-04-30",
        "routing": "", "validNote": "", "currency": "USD", "total": 4050,
        "lines": [{"charge": "OCEAN FREIGHT", "category": "freight", "unit": "per_container", "equipment": "40'HQ",
                   "rate": 1800, "min": None, "qty": 2, "amount": 3600},
                  {"charge": "DRAYAGE (per TRIP)", "category": "inland", "unit": "other", "rate": 450, "min": None,
                   "qty": 1, "amount": 450},
                  {"charge": "DUTIES AND TAXES (AT COST)", "category": "customs", "unit": "per_shipment", "rate": None,
                   "min": None, "qty": None, "amount": None}],
        "notSaid": ["fuel", "minimum"], "notes": "Subject to space", "caseStatus": "New", "usedOnWon": False,
        "withdrawn": False, "withdrawnNote": ""}
    assert "pol" not in doc


def test_a_two_route_case_lcl_and_air(world):
    w = world
    rates(w, "r2", "OPP-9901", "Belden Air", 900, route=1, lines=[FCL_LINES[1]])
    rates(w, "r3", "OPP-9901", "Belden Air", 1200, route=2, lines=[dict(FCL_LINES[0], unit="KG", charge="AIR FREIGHT")])
    docs = built(w)
    a, b = docs["OPP-9901_A_belden-air_1"], docs["OPP-9901_B_belden-air_1"]
    assert (a["mode"], a["cargo"], a["scope"], a["incoterm"]) == \
        ("LCL", {"cbm": 3.0, "kg": 500.0, "commodity": "Lamps", "dg": False}, "P2P", "EXW")
    assert (b["mode"], b["cargo"], b["incoterm"], b["destination"]["iata"]) == \
        ("Air", {"pieces": 2, "kg": 100.0, "chargeableKg": 200.0, "commodity": "Lamp shades", "dg": False}, "EXW", "SCL")
    assert b["lines"][0]["unit"] == "per_kg" and b["vendor"] == {"key": "Belden Air", "name": "Belden Air", "country": "",
                                                                  "kind": "agent"}
    assert (a["caseStatus"], "pod" in a) == ("Quoted", False)


def test_a_correction_and_new_rates(world):
    w = world
    rates(w, "r4", "OPP-9900", "Ostrava Shipping S.A.", 4050, on="2027-03-01")
    rates(w, "r5", "OPP-9900", "Ostrava Shipping S.A.", 3990, on="2027-03-01",
          at=dt.datetime(2027, 3, 2, 16, 0, tzinfo=dt.timezone.utc))                    # same day: a correction
    docs = built(w)
    assert list(docs) == ["OPP-9900_A_ostrava-shipping-s-a_1"]
    assert (docs["OPP-9900_A_ostrava-shipping-s-a_1"]["rev"], docs["OPP-9900_A_ostrava-shipping-s-a_1"]["total"]) == (2, 3990)
    rates(w, "r6", "OPP-9900", "Ostrava Shipping S.A.", 3800, on="2027-03-04",
          at=dt.datetime(2027, 3, 4, 15, 0, tzinfo=dt.timezone.utc))                    # another day: new rates
    docs = built(w)
    assert sorted(docs) == ["OPP-9900_A_ostrava-shipping-s-a_1", "OPP-9900_A_ostrava-shipping-s-a_2"]
    assert (docs["OPP-9900_A_ostrava-shipping-s-a_2"]["seq"], docs["OPP-9900_A_ostrava-shipping-s-a_2"]["rev"]) == (2, 1)


def test_a_strike_every_version_struck_and_put_back(world):
    w = world
    first = rates(w, "r7", "OPP-9900", "Ostrava Shipping S.A.", 4050, on="2027-03-01")
    second = rates(w, "r8", "OPP-9900", "Ostrava Shipping S.A.", 3990, on="2027-03-01",
                   at=dt.datetime(2027, 3, 2, 16, 0, tzinfo=dt.timezone.utc))
    repo = db.Repository(w["conn"], source="ui")
    site_rates.strike(repo, second)
    doc = built(w)["OPP-9900_A_ostrava-shipping-s-a_1"]
    assert (doc["rev"], doc["total"], doc["withdrawn"]) == (1, 4050, False)                     # rev back
    site_rates.strike(repo, first)
    doc = built(w)["OPP-9900_A_ostrava-shipping-s-a_1"]
    assert (doc["rev"], doc["total"], doc["withdrawn"], doc["withdrawnNote"]) == (0, 3990, True, "Struck out in OppList")
    # the site sees which vendorRates are struck out
    case = fs.build(w["conn"], today=TODAY).cases["OPP-9900"]
    assert case["rx"] == ["r7", "r8"]
    assert w["conn"].execute("SELECT source FROM change_log WHERE table_name = 'vendor_rate' AND field = 'voided' "
                             "ORDER BY id DESC").fetchone()[0] == "ui"
    site_rates.strike(repo, second, voided=False)                                                # Put back
    doc = built(w)["OPP-9900_A_ostrava-shipping-s-a_1"]
    assert (doc["rev"], doc["total"], doc["withdrawn"]) == (1, 3990, False)
    assert fs.build(w["conn"], today=TODAY).cases["OPP-9900"]["rx"] == ["r7"]


def test_won_with_the_awarded_vendor_and_a_status_change_rewrites_rev_unchanged(world):
    w = world
    rates(w, "r9", "OPP-9900", "Ostrava Shipping S.A.", 4050, on="2027-03-01")
    client = FakeFirestore(project="demo-rate-card")
    assert push(w, client).written == 1
    repo = db.Repository(w["conn"], source="ui")
    repo.set_status(w["ids"][9900], "Won", "2027-03-05")
    repo.update("enquiry", w["ids"][9900], {"awarded_vendor_id": w["ids"]["ostrava"]})
    repo.commit()
    result = push(w, client)
    doc = client.docs["agentQuotes/OPP-9900_A_ostrava-shipping-s-a_1"]
    assert result.written == 1 and (doc["caseStatus"], doc["usedOnWon"], doc["rev"]) == ("Won", True, 1)
    assert doc["pushedAt"] == "REQUEST_TIME"


def test_a_duplicate_case_is_withdrawn(world):
    w = world
    rates(w, "r10", "OPP-9902", "Ostrava Shipping S.A.", 2000)
    repo = db.Repository(w["conn"], source="ui")
    repo.set_status(w["ids"][9902], "Dropped", "2027-03-05", closed_reason="Duplicate of OPP-9900")
    repo.commit()
    doc = built(w)["OPP-9902_A_ostrava-shipping-s-a_1"]
    assert (doc["caseStatus"], doc["withdrawn"], doc["withdrawnNote"]) == ("Closed", True, "The case was a duplicate")


# --------------------------------------------------------------- the push --
def test_unchanged_not_written_a_rename_keeps_its_id_and_a_failed_commit_comes_next_run(world):
    w = world
    rates(w, "r11", "OPP-9900", "Ostrava Shipping S.A.", 4050)
    failing = FakeFirestore(project="demo-rate-card", fail_on=0)
    with pytest.raises(fs.FirebaseError):
        push(w, failing)
    client = FakeFirestore(project="demo-rate-card")
    assert push(w, client).written == 1                          # what failed is written on the next run
    assert push(w, client).written == 0                          # unchanged: not again
    repo = db.Repository(w["conn"], source="ui")
    repo.update("party", w["ids"]["ostrava"], {"key": "Ostrava Lines"})
    repo.commit()
    result = push(w, client)
    assert result.written == 1 and list(client.docs) == ["agentQuotes/OPP-9900_A_ostrava-shipping-s-a_1"]
    assert client.docs["agentQuotes/OPP-9900_A_ostrava-shipping-s-a_1"]["vendor"]["key"] == "Ostrava Lines"
    assert all(path.startswith("agentQuotes/") for commit in client.commits for path in commit)


def test_the_store_switched_off_is_off_and_writes_nothing(world, tmp_path, monkeypatch):
    w = world
    rates(w, "r12", "OPP-9900", "Ostrava Shipping S.A.", 4050)
    conn = w["conn"]
    off = "the Rate Card's local store is off (Settings, Team site)"
    assert ratecard_sync.configured(conn) == (False, off)
    assert ratecard_sync.status_line(conn) == "Off: {}".format(off)
    monkeypatch.setattr(fs, "is_live", lambda path: True)
    made = []
    monkeypatch.setattr(ratecard_sync, "client_for", lambda c: made.append(1) or FakeFirestore())
    result = ratecard_sync.run(conn, db_path=w["path"], state_file=w["state"])
    assert result.off == off and made == []
    db.set_setting(conn, ratecard_sync.STORE_SETTING, "on")
    conn.commit()
    from opplist import paths
    assert ratecard_sync.configured(conn) == (True, "")
    assert ratecard_sync.service_account(conn) == "local store in {}".format(paths.DATA_DIR / "rate-card")
    # switched on, the push goes to the store's own folder, never the team site's
    monkeypatch.undo()
    client = ratecard_sync.client_for(conn)
    assert client.project == "demo-rate-card" and client.folder == paths.DATA_DIR / "rate-card"
    assert not hasattr(ratecard_sync, "key_path")


def test_the_sync_run_pushes_after_the_team_site_and_each_failure_is_its_own(world, tmp_path):
    w = world
    rates(w, "r13", "OPP-9900", "Ostrava Shipping S.A.", 4050)
    team, card = FakeFirestore(), FakeFirestore(project="demo-rate-card")
    result = fs.sync(w["path"], client=team, state_file=tmp_path / "team.json", today=TODAY,
                     reports_dir=tmp_path / "reports", record=tmp_path / "exports.json", ratecard_client=card,
                     ratecard_state=w["state"])
    assert result.ok and result.ratecard.ok and result.ratecard.written == 1
    assert list(card.docs) == ["agentQuotes/OPP-9900_A_ostrava-shipping-s-a_1"]
    assert not any(p.startswith("agentQuotes/") for c in team.commits for p in c)
    team_writes = fs.writes_on(fs.load_state(tmp_path / "team.json"), TODAY)
    assert team_writes == sum(len(c) for c in team.commits)                          # the cap counts the team site's
    assert "Rate Card: 1 document written, 1 agent quote in all" in result.lines()
    # the team site failing never stops the Rate Card, nor the other way round
    broken_team = FakeFirestore(fail_on=0)
    broken_card = FakeFirestore(project="demo-rate-card", fail_on=0)
    rates(w, "r14", "OPP-9900", "Ostrava Shipping S.A.", 3900, on="2027-03-04")
    result = fs.sync(w["path"], client=broken_team, state_file=tmp_path / "team2.json", today=TODAY,
                     reports_dir=tmp_path / "reports", record=tmp_path / "exports.json", ratecard_client=card,
                     ratecard_state=w["state"])
    assert not result.ok and result.ratecard.ok and result.ratecard.written == 1
    result = fs.sync(w["path"], client=FakeFirestore(), state_file=tmp_path / "team3.json", today=TODAY,
                     reports_dir=tmp_path / "reports", record=tmp_path / "exports.json", ratecard_client=broken_card,
                     ratecard_state=tmp_path / "card2.json")
    assert result.ok and not result.ratecard.ok and "503" in result.ratecard.error


# ------------------------------------------------ 3, 4. the case page and Settings --
def test_strike_out_and_put_back_on_the_case_page_and_the_settings_line(world):
    w = world
    rate_id = rates(w, "r15", "OPP-9900", "Ostrava Shipping S.A.", 4050)
    app = create_app(w["path"])
    app.config["TESTING"] = True
    client = app.test_client()
    page = client.get("/enquiries/{}".format(w["ids"][9900])).data.decode("utf-8")
    assert 'action="/enquiries/{}/rates/{}"'.format(w["ids"][9900], rate_id) in page and "leave the Rate Card" in page
    page = client.post("/enquiries/{}/rates/{}".format(w["ids"][9900], rate_id), data={"action": "void"},
                       follow_redirects=True).data.decode("utf-8")
    assert "struck out." in page and "1 struck out" in page and ">Put back<" in page
    page = client.post("/enquiries/{}/rates/{}".format(w["ids"][9900], rate_id), data={"action": "unvoid"},
                       follow_redirects=True).data.decode("utf-8")
    assert "put back." in page and "USD 4,050" in page
    settings = client.get("/settings").data.decode("utf-8")
    assert "<dt>Rate Card</dt>" in settings and "Off: the Rate Card&#39;s local store is off" in settings


# ------------------------------------------------------- 5. the role coloader --
def test_the_role_coloader_on_the_party_page_and_on_ask_vendors_list(world):
    w = world
    assert web_module.PARTY_ROLES.index("coloader") == web_module.PARTY_ROLES.index("carrier_ocean") + 1
    assert migrate_master.ROLES.index("coloader") == migrate_master.ROLES.index("carrier_ocean") + 1
    app = create_app(w["path"])
    app.config["TESTING"] = True
    page = app.test_client().get("/parties/{}".format(w["ids"]["belden"])).data.decode("utf-8")
    assert 'name="roles" value="coloader"' in page
    rows = {r["key"]: r for r in firebase_vendors.rows(w["conn"], w["path"].parent / "none" / "mail.db")[0]}
    belden = rows["Belden Air"]                                     # an agent never asked, also a coloader
    assert (belden["type"], belden["roles"], belden["asked"]) == ("Agent", ["agent", "coloader"], 0)
    assert "Corvane Logistics" not in rows                          # an agent with no such role is not


# ---------------------------------------------------------- 6. the site's words --
def test_the_sites_words_logged_by_and_changed_by_while_8765_keeps_its_own(world):
    line = fs._event({"note": "Called them", "source": "web", "kind": "note", "status": "New", "reason": None,
                      "at": "2027-03-02", "seq": None, "channel": None}, logged_by="Dee Rowan")
    assert line["sub"] == "Called them · logged by Dee Rowan"
    item = {"by": "Ava", "accepted": "Iris"}
    assert site_edit.headline(item, on_site=True) == "Changed by Ava, accepted by Iris"
    assert site_edit.headline(item) == "Changed on the team site by Ava, accepted by Iris"       # 8765
