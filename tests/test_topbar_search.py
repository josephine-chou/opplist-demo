"""Batch 13e: the search box in the top bar. Every company, case, number
and reference here is invented."""

from __future__ import annotations

import re

import pytest

from conftest import add_party, seed_reference
from opplist import db, shipments
from opplist.web import create_app


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "search.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    heron = add_party(repo, "Heron Quay Freight", country="Chile")
    # a party merged into Heron Quay: its old name is now one of the aliases
    old = add_party(repo, "Saltmarsh Cargo", country="Chile")
    repo.merge_party(heron, old)
    ids = {}
    for opp, status, commodity in (("OPP-2301", "New", "linen bales"),
                                   ("OPP-2302", "Won", "brass valves"),
                                   ("OPP-2303", "Lost", "cork boards")):
        ids[opp] = repo.insert("enquiry", {
            "opp_no": opp, "requester_party_id": heron, "received_on": "2026-11-02",
            "status": status, "service_mode": "Air", "origin_country": "USA", "origin_loc": "MIA",
            "dest_country": "Chile", "dest_loc": "SAI", "commodity": commodity})
    sid = shipments.start(repo, ids["OPP-2302"])
    shipments.edit(repo, sid, {"awb": "731-55120984", "booking_no": "BKG 77 410"})
    repo.commit()
    conn.close()
    application = create_app(path)
    application.config["TESTING"] = True
    application.config["IDS"] = ids
    return application


def go(client, **args):
    return client.get("/search", query_string=args)


@pytest.mark.parametrize("typed", ["2301", "02301", "OPP-2301", "opp 2301", "OPP2301"])
def test_an_opp_number_opens_the_case_in_every_spelling(app, typed):
    page = go(app.test_client(), q=typed, scope="all")
    assert page.status_code == 302
    assert page.headers["Location"].endswith("/enquiries/{}".format(app.config["IDS"]["OPP-2301"]))


def test_a_short_number_and_open_scope_jump_too(app):
    client = app.test_client()
    # 'opp 2303' in Open cases still opens the (closed) case: the number is the case
    page = go(client, q="opp 2303", scope="open")
    assert page.headers["Location"].endswith("/enquiries/{}".format(app.config["IDS"]["OPP-2303"]))


def test_a_number_that_is_not_a_case_falls_through(app):
    page = go(app.test_client(), q="OPP-2399", scope="all")
    assert page.status_code == 302
    assert "/enquiries?" in page.headers["Location"] and "q=OPP-2399" in page.headers["Location"]
    assert "view=all" in page.headers["Location"]


def test_each_scope_lands_on_its_page_with_q(app):
    client = app.test_client()
    assert re.search(r"/enquiries\?.*view=all", go(client, q="linen", scope="all").headers["Location"])
    assert re.search(r"/enquiries\?.*view=open", go(client, q="linen", scope="open").headers["Location"])
    assert go(client, q="Heron", scope="parties").headers["Location"].endswith("/parties?q=Heron")
    assert go(client, q="", scope="all").headers["Location"].endswith("/enquiries")


def test_the_awb_of_a_shipment_is_found_with_or_without_the_hyphen(app):
    client = app.test_client()
    for typed in ("731-55120984", "73155120984", "55120984"):
        body = client.get("/enquiries", query_string={"view": "all", "q": typed}).data.decode("utf-8")
        assert "OPP-2302" in body and "OPP-2301" not in body, typed
        assert "AWB 731-55120984 · shipment" in body, typed
    body = client.get("/enquiries", query_string={"view": "all", "q": "BKG77410"}).data.decode("utf-8")
    assert "Booking BKG 77 410 · shipment" in body


def test_an_alias_is_found_on_parties(app):
    conn = db.open_db(app.config["DB_PATH"])
    repo = db.Repository(conn, source="test")
    plover = add_party(repo, "Plover Lines", country="USA", aliases='["Plover Ocean Services"]')
    repo.commit()
    conn.close()
    body = app.test_client().get("/parties", query_string={"q": "Ocean Services"}).data.decode("utf-8")
    assert "Plover Lines" in body and "Heron Quay Freight" not in body
    assert plover


def test_a_merged_party_s_old_name_finds_the_one_it_went_into(app):
    body = app.test_client().get("/parties", query_string={"q": "Saltmarsh"}).data.decode("utf-8")
    assert "Heron Quay Freight" in body


@pytest.mark.parametrize("path", ["/enquiries", "/parties", "/locations", "/tracking", "/shipments",
                                  "/dashboard", "/report", "/export", "/settings", "/enquiries/new"])
def test_the_box_is_on_every_page(app, path):
    body = app.test_client().get(path).data.decode("utf-8")
    assert 'id="tb-search"' in body, path
    assert 'placeholder="OPP no., company, subject, ref, AWB"' in body
    assert '<option value="all" selected>All cases</option>' in body


def test_the_box_keeps_the_q_on_enquiries_and_parties(app):
    client = app.test_client()
    body = client.get("/enquiries", query_string={"view": "open", "q": "linen"}).data.decode("utf-8")
    assert 'id="tb-search-q" name="q" value="linen"' in body
    assert '<option value="open" selected>Open cases</option>' in body
    body = client.get("/parties", query_string={"q": "Heron"}).data.decode("utf-8")
    assert 'id="tb-search-q" name="q" value="Heron"' in body
    assert '<option value="parties" selected>Parties</option>' in body
    body = client.get("/tracking", query_string={"q": "linen"}).data.decode("utf-8")
    assert 'id="tb-search-q" name="q" value=""' in body
