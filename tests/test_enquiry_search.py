"""The Enquiries search box reaching past the case itself (2026-09-16):
vendor references, vendor names, notes, free text, the requester's people."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, enquiry_search
from opplist.web import create_app


@pytest.fixture
def web(tmp_path):
    path = tmp_path / "search.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile", full_name="Tolvex Chile SpA",
                       aliases='["Total Vertex Solutions"]')
    craft = add_party(repo, "Vanta Multimodal", country="Brazil", full_name="Vanta Multimodal Ltda")
    seavio = add_party(repo, "Seavio", country="USA")
    repo.insert("contact", {"party_id": tolvex, "name": "Renata Solis", "email": "renata@tolvex.example"})
    a = add_enquiry(repo, "OPP-1407", tolvex, status="Quoted", received="2026-08-20",
                    origin_loc="MIA", dest_loc="SAI", commodity="Machine parts",
                    remarks="consignee needs a tail-lift truck in Pudahuel")
    repo.insert("vendor_request", {"enquiry_id": a, "vendor_party_id": craft,
                                   "asked_on": "2026-08-21", "their_ref": "KQRS 64829",
                                   "gist": "USD 2,760 all in", "notes": "offer holds two weeks"})
    repo.insert("vendor_request", {"enquiry_id": a, "vendor_party_id": seavio,
                                   "asked_on": "2026-08-21", "their_ref": "SHP-77", "voided": 1})
    repo.insert("status_event", {"enquiry_id": a, "at": "2026-08-25", "status": "Quoted",
                                 "kind": "note", "note": "rang Renata, she is waiting on Brisamar"})
    repo.insert("status_event", {"enquiry_id": a, "at": "2026-08-26", "status": "Quoted",
                                 "kind": "note", "note": "voided text ZEBRA", "voided": 1})
    b = add_enquiry(repo, "OPP-1408", tolvex, status="New", received="2026-08-22",
                    origin_loc="LAX", dest_loc="SAI", commodity="Ceramic tiles")
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path


def page(client, q):
    return client.get("/enquiries?view=all&q=" + q).data.decode("utf-8")


def test_the_loose_reference():
    assert enquiry_search.loose("KQRS 64829") == "KQRS64829"
    assert enquiry_search.loose("KQRS-64829") == "KQRS64829"
    assert enquiry_search.loose(" kqrs - 64 829 ") == "kqrs64829"


def test_a_vendor_reference_is_found_however_it_is_written(web):
    client, path = web
    for q in ("KQRS64829", "KQRS%2064829", "KQRS-64829", "kqrs64829", "64829"):
        body = page(client, q)
        assert "OPP-1407" in body, q
        assert "OPP-1408" not in body, q
        assert "KQRS 64829 · Vanta Multimodal their ref" in body, q
    # the vendor's own name, full name included; and its gist / notes
    for q in ("Vanta", "Multimodal%20Ltda", "all%20in", "holds%20two"):
        assert "OPP-1407" in page(client, q), q
    assert "Vanta Multimodal · vendor asked" in page(client, "Vanta")
    assert "Vanta Multimodal · vendor note: USD 2,760 all in" in page(client, "all%20in")


def test_a_voided_vendor_row_does_not_count(web):
    client, path = web
    assert "OPP-1407" not in page(client, "SHP-77")
    assert "OPP-1407" not in page(client, "SHP77")
    assert "OPP-1407" not in page(client, "Seavio")


def test_a_timeline_note_is_found_but_not_a_voided_one(web):
    client, path = web
    body = page(client, "Brisamar")
    assert "OPP-1407" in body and "OPP-1408" not in body
    assert "note: rang Renata, she is waiting on Brisamar" in body
    assert "OPP-1407" not in page(client, "ZEBRA")


def test_the_free_text_the_requester_names_and_the_contacts(web):
    client, path = web
    body = page(client, "Pudahuel")
    assert "OPP-1407" in body and "remarks: consignee needs a tail-lift truck in Pudahuel" in body
    # the requester's full name and aliases: both cases, both explained
    body = page(client, "Total%20Vertex")
    assert "OPP-1407" in body and "OPP-1408" in body
    assert "requester also known as: Total Vertex Solutions" in body
    body = page(client, "renata@tolvex.example")
    assert "OPP-1407" in body and "contact: Renata Solis renata@tolvex.example" in body


def test_a_core_hit_gets_no_explanation(web):
    client, path = web
    body = page(client, "1407")
    assert "OPP-1407" in body and "q-hit" not in body
    body = page(client, "Machine")
    assert "OPP-1407" in body and "q-hit" not in body
    # and one search string can hit both ways: the hit line only on the other row
    body = page(client, "Tolvex")
    assert "OPP-1407" in body and "OPP-1408" in body and "q-hit" not in body


def test_a_long_note_is_cut_to_sixty_characters(web):
    client, path = web
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    case = conn.execute("SELECT id FROM enquiry WHERE opp_no = 'OPP-1408'").fetchone()["id"]
    repo.insert("status_event", {"enquiry_id": case, "at": "2026-08-27", "status": "New",
                                 "kind": "note", "note": "GIRAFFE " + "x" * 100})
    repo.commit()
    conn.close()
    body = page(client, "GIRAFFE")
    assert "note: GIRAFFE " + "x" * 52 + "..." in body
