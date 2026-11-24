"""Batch S1: the Service decides which places a route needs.

The incoterm says who pays for which leg between buyer and seller; the
Service says which legs the requester wants from us. With a Service of the
four, it decides (Door to door and Door to port: the pickup place; Door to
door and Port to door: the delivery place; Port to port: neither). With no
Service, or one outside the four, the incoterm decides as before, except
that FAS no longer needs a pickup place. Every value here is invented;
cases from OPP-3701.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db
from opplist.web import create_app, forms

TODAY = "2026-11-18"
JS = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.js").read_text(encoding="utf-8")
SERVICES = (None, "Door to door", "Door to port", "Port to door", "Port to port")


def gaps(incoterm, service, **places):
    values = {"requester_party_id": 1, "received_on": "2026-11-16", "incoterm": incoterm,
              "service_scope": service}
    values.update(places)
    return {k: v for k, v in forms.missing(values).items() if k in ("origin_alt", "dest_alt")}


# -------------------------------------------------------------- the rule --
def test_exw_port_to_port_needs_neither_place():
    assert gaps("EXW", "Port to port") == {}


def test_exw_door_to_port_needs_the_pickup_place():
    assert gaps("EXW", "Door to port") == {"origin_alt": "Door to port needs the pickup place"}
    assert gaps("EXW", "Door to port", origin_alt="Unit 4, Brannock Yard") == {}


def test_fob_door_to_door_needs_both():
    assert gaps("FOB", "Door to door") == {"origin_alt": "Door to door needs the pickup place",
                                           "dest_alt": "Door to door needs the delivery place"}


def test_dap_port_to_port_needs_neither():
    assert gaps("DAP", "Port to port") == {}
    assert gaps("DAP", "Port to door") == {"dest_alt": "Port to door needs the delivery place"}


def test_with_no_service_the_incoterm_decides_and_fas_needs_nothing():
    assert gaps("EXW", None) == {"origin_alt": "EXW needs the pickup place"}
    assert gaps("FCA", "") == {"origin_alt": "FCA needs the pickup place"}
    assert gaps("DDP", None) == {"dest_alt": "DDP needs the delivery place"}
    assert gaps("FAS", None) == {}
    assert gaps("FOB", None) == {}


def test_a_service_outside_the_four_falls_back_to_the_incoterm():
    assert gaps("EXW", "Door to rail") == {"origin_alt": "EXW needs the pickup place"}
    assert gaps("CIF", "Door to rail") == {}
    # the four are read whatever the case and spacing Settings gave them
    assert gaps("CIF", " door to PORT ") == {"origin_alt": "Door to port needs the pickup place"}


def test_field_state_agrees_with_missing_for_every_combination():
    for incoterm, service in itertools.product(forms.INCOTERMS, SERVICES + ("Door to rail",)):
        state = forms.field_state("FCL", incoterm, service)
        found = gaps(incoterm, service)
        for name in ("origin_alt", "dest_alt"):
            assert (state[name] == "required") == (name in found), (incoterm, service, name)
            assert state[name] in ("required", "dim")


def test_app_js_mirrors_the_python_rule():
    js_origin = json.loads(re.search(r"var ORIGIN_ALT = (\[[^\]]*\]);", JS).group(1))
    js_dest = json.loads(re.search(r"var DEST_ALT = (\[[^\]]*\]);", JS).group(1))
    assert set(js_origin) == forms.ORIGIN_ALT_INCOTERMS and set(js_dest) == forms.DEST_ALT_INCOTERMS
    block = re.search(r"var SERVICE_PLACES = \{(.*?)\};", JS, re.S).group(1)
    js_places = {k: (a == "true", b == "true") for k, a, b in
                 re.findall(r'"([a-z ]+)": \[(true|false), (true|false)\]', block)}
    assert js_places == {k.casefold(): v for k, v in forms.SERVICE_PLACES.items()}
    assert "service_scope)$" in JS                      # the Service select repaints the form


# ------------------------------------------------------------ the pages --
@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "s1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Harrowgate Shipping", country="Brazil")
    case = add_enquiry(repo, "OPP-3701", party, received="2026-11-16", service_mode="FCL",
                       container_type="40HQ", container_qty=2, incoterm="EXW", origin_loc="LAX", origin_country="USA",
                       dest_loc="NGB", dest_country="China", commodity="Garden benches")
    route_a = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (case,)).fetchone()[0]
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, {"party": party, "case": case, "route_a": route_a}


def register_form(**changes):
    values = {"received_on": "2026-11-16", "requester_party_id": "1", "service_mode": "FCL",
              "container_type": "40HQ", "container_qty": "2", "incoterm": "EXW", "commodity": "Garden benches",
              "origin_country": "USA", "origin_loc": "LAX", "dest_country": "China", "dest_loc": "NGB",
              "origin_alt": "", "dest_alt": "", "service_scope": "", "decision": "new",
              "source_subject": "Rates please, benches LAX to NGB"}
    values.update(changes)
    return values


def count_cases(path):
    conn = db.open_db(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0]
    finally:
        conn.close()


def test_the_register_page_takes_exw_port_to_port_without_places(world):
    client, path, ids = world
    answer = client.post("/enquiries/new", data=register_form(service_scope="Port to port"))
    assert answer.status_code == 302, answer.data.decode("utf-8")[:2000]
    assert count_cases(path) == 2
    conn = db.open_db(path)
    row = conn.execute("SELECT r.service_scope, r.incoterm FROM enquiry_route r JOIN enquiry e "
                       "ON e.id = r.enquiry_id WHERE e.opp_no <> 'OPP-3701'").fetchone()
    conn.close()
    assert tuple(row) == ("Port to port", "EXW")


def test_the_register_pages_refusal_names_the_service(world):
    client, path, ids = world
    answer = client.post("/enquiries/new", data=register_form(service_scope="Door to port", incoterm="FOB"))
    body = answer.data.decode("utf-8")
    assert answer.status_code == 400
    assert "Door to port needs the pickup place" in body
    assert count_cases(path) == 1
    # with no Service the incoterm still asks, and says so
    body = client.post("/enquiries/new", data=register_form()).data.decode("utf-8")
    assert "EXW needs the pickup place" in body
    # the refused page greys by the Service it was sent with
    body = client.post("/enquiries/new", data=register_form(service_scope="Port to door")).data.decode("utf-8")
    assert re.search(r'class="f required" data-field="dest_alt"', body)
    assert re.search(r'class="f dim" data-field="origin_alt"', body)


def route_post(client, ids, **changes):
    data = {"form": "route", "route_id": str(ids["route_a"]), "origin_country": "USA", "origin_loc": "LAX",
            "dest_country": "China", "dest_loc": "NGB", "service_mode": "FCL", "incoterm": "EXW",
            "service_scope": "", "origin_alt": "", "dest_alt": ""}
    data.update(changes)
    return client.post("/enquiries/{}/routes".format(ids["case"]), data=data,
                       follow_redirects=True).data.decode("utf-8")


def test_the_case_pages_still_missing_line_follows_the_service(world):
    client, path, ids = world
    body = route_post(client, ids, service_scope="Port to port")
    assert "A saved" in body and "Still missing" not in body
    body = route_post(client, ids, service_scope="Door to port")
    assert "Still missing: Origin alt (pickup), Door to port needs the pickup place" in body
    body = route_post(client, ids, service_scope="")
    assert "Origin alt (pickup), EXW needs the pickup place" in body
    # the route editor greys by the route's Service
    body = route_post(client, ids, service_scope="Port to door", incoterm="FOB")
    assert "Port to door needs the delivery place" in body
    editor = body[body.index('id="routes"'):]
    assert 'class="f required" data-field="dest_alt"' in editor
