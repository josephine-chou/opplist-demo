"""Registering must not lose what she typed, and lists sort.

The complaint these answer: 'I fill the form, press Save, and it is gone.'
"""

from __future__ import annotations

import datetime as dt

import pytest

import enquiries_client
from conftest import add_party, seed_reference
from opplist import db, logs
from opplist.web import create_app

TODAY = dt.date.today().isoformat()


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "saving.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    enquiry_id = repo.insert("enquiry", {
        "opp_no": "OPP-1401", "requester_party_id": tolvex,
        "received_on": "2026-09-01", "service_mode": "FCL",
        "container_type": "40HQ", "container_qty": 2,
        "origin_country": "USA", "origin_loc": "LAX",
        "dest_country": "Chile", "dest_loc": "SAI",
        "commodity": "PET flakes", "status": "New"})
    repo.insert("status_event", {"enquiry_id": enquiry_id, "at": "2026-09-01",
                                 "status": "New"})
    db.set_setting(conn, "next_opp_number", 1402)
    repo.commit()
    conn.close()
    application = create_app(path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def conn_of(app):
    conn = db.connect(app.config["DB_PATH"])
    return conn


def full_form(**overrides):
    values = {
        "received_on": TODAY, "requester_party_id": "1", "owner": "Victor",
        "service_mode": "Air", "air_weight_kg": "216",
        "origin_country": "USA", "origin_loc": "LAX",
        "dest_country": "Chile", "dest_loc": "SAI",
        "commodity": "Hand tools", "remarks": "note to keep",
        "decision": "new",
    }
    values.update(overrides)
    return values


# ------------------------------------------------- a. validation failure ----
# test_a_rejected_save_keeps_every_field_she_typed: replaced in 15c by tests/test_register_v3.py (the register page is the case page's cards).


def test_a_rejected_save_says_which_field_and_why(client):
    body = client.post("/enquiries/new", data=full_form(
        air_weight_kg="about 200 kg")).data.decode("utf-8")
    assert "Not saved - fix these first" in body
    assert "air weight kg" in body and "must be a number" in body
    assert "Everything you typed is still here" in body


def test_a_missing_requester_is_named_too(client):
    body = client.post("/enquiries/new", data=full_form(
        requester_party_id="")).data.decode("utf-8")
    assert "requester party id" in body and "pick who is asking" in body


def test_the_inline_add_party_text_survives_a_rejection(client):
    body = client.post("/enquiries/new", data=full_form(
        air_weight_kg="nope", new_party_key="Brand New Agent",
        new_origin_loc="ZZZ")).data.decode("utf-8")
    assert 'value="Brand New Agent"' in body
    assert 'value="ZZZ"' in body


# ------------------------------------------------- b. the duplicate panel ---
def _same_as_existing(**overrides):
    """The same request as OPP-1401, so the duplicate panel comes up."""
    values = full_form(decision="", service_mode="FCL", air_weight_kg="",
                       container_type="40HQ", container_qty="2",
                       origin_loc="LAX", dest_loc="SAI",
                       commodity="PET flakes", received_on="2026-09-01")
    values.update(overrides)
    return values


# test_the_panel_says_nothing_has_been_saved: DD1 (2026-11-12) says it once, 'Not saved yet: pick one.';
# its test is in tests/test_dd1.py.


def test_the_panel_keeps_the_form_underneath_it(client):
    body = client.post("/enquiries/new", data=_same_as_existing(
        remarks="typed before the panel")).data.decode("utf-8")
    assert "typed before the panel" in body


def test_nothing_is_written_while_the_panel_is_up(client, app):
    client.post("/enquiries/new", data=_same_as_existing())
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"] == 1
    conn.close()


# ------------------------------------------------------- c. and d. saving ---
def test_a_good_save_lands_on_the_case_page_with_a_green_banner(client, app):
    response = client.post("/enquiries/new", data=full_form(),
                           follow_redirects=True)
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "saved-banner" in body and "OPP-1402" in body
    assert 'data-clear-draft="new-enquiry"' in body     # the draft is done with
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"] == 2
    conn.close()


def test_the_form_carries_the_draft_hooks(client):
    body = client.get("/enquiries/new").data.decode("utf-8")
    assert 'data-draft-key="new-enquiry"' in body
    assert "Restore unsaved draft?" in body


def test_the_browser_side_does_what_it_says(client):
    """The draft is in app.js; check the pieces are there and self-consistent."""
    script = client.get("/static/app.js").data.decode("utf-8")
    assert "opplist:draft:" in script
    assert "DRAFT_EVERY = 2000" in script              # every two seconds
    assert "beforeunload" in script
    assert "localStorage" in script
    assert "data-clear-draft" in script


# ------------------------------------------------------------- e. logging ---
def test_every_registration_attempt_reaches_the_log(client, tmp_path,
                                                    monkeypatch):
    written = []
    monkeypatch.setattr(logs.get("web"), "info",
                        lambda msg, *args: written.append(msg % args))

    client.post("/enquiries/new", data=full_form())                  # created
    client.post("/enquiries/new", data=full_form(air_weight_kg="x")) # rejected
    client.post("/enquiries/new", data=_same_as_existing())

    lines = [line for line in written if line.startswith("register:")]
    assert len(lines) == 3
    assert "created OPP-1402" in lines[0]
    assert "rejected: air_weight_kg must be a number" in lines[1]
    assert "possible duplicate panel shown" in lines[2]
    assert "nothing saved" in lines[2]
    for line in lines:
        assert "requester=" in line and "received=" in line


# ---------------------------------------------------------- 2. sorting -----

def test_the_headings_are_links_that_sort(client):
    """Enquiries v2 (2026-09-25): the Sort control, in the browser; the
    three sorts it offers, and ?sort= still read for the rest."""
    body = client.get("/enquiries").data.decode("utf-8")
    assert 'data-sort="received_on"' in body and 'data-sort="requester"' in body
    assert 'data-sort="last_event_on"' in body
    assert 'data-s-status="' in body                        # ?sort=status still has its values



def test_clicking_again_reverses_it(client, app):
    add = conn_of(app)
    repo = db.Repository(add, source="test")
    zzz = add_party(repo, "Zebra Cargo", country="USA")
    repo.insert("enquiry", {"opp_no": "OPP-1500", "requester_party_id": zzz,
                            "received_on": "2026-01-01", "status": "New"})
    repo.commit()
    add.close()
    asc = enquiries_client.get(client, "/enquiries?view=all&group=status&sort=requester&dir=asc").shown
    desc = enquiries_client.get(client, "/enquiries?view=all&group=status&sort=requester&dir=desc").shown
    assert set(asc) == set(desc) and asc != desc



def test_the_sort_actually_changes_the_order(client, app):
    conn = conn_of(app)
    repo = db.Repository(conn, source="test")
    zzz = add_party(repo, "Zebra Cargo", country="USA")
    repo.insert("enquiry", {"opp_no": "OPP-1500", "requester_party_id": zzz,
                            "received_on": "2026-01-01", "status": "New"})
    repo.commit()
    conn.close()

    def order(direction):
        # both are New: one status group, so the order is the sort's
        shown = enquiries_client.get(client, "/enquiries?view=all&group=status&sort=requester&dir=" + direction).shown
        return shown.index("OPP-1401"), shown.index("OPP-1500")

    tolvex_first, zebra_first = order("asc")
    assert tolvex_first < zebra_first
    tolvex_last, zebra_last = order("desc")
    assert zebra_last < tolvex_last



def test_the_last_sort_is_remembered(client, app):
    """v2 (2026-09-25): the last group and sort are the browser's
    (localStorage), not a setting; the server writes nothing."""
    client.get("/enquiries?sort=requester&dir=asc")
    conn = conn_of(app)
    assert db.get_setting(conn, "sort_enquiries") in (None, "")
    conn.close()
    js = client.get("/static/app.js").data.decode("utf-8")
    assert 'KEY = "opplist.enquiries"' in js and "localStorage.setItem(KEY" in js
    # coming back without parameters: the remembered sort is applied
    body = client.get("/enquiries").data.decode("utf-8")
    remembered = enquiries_client.read(body, "/enquiries", {"sort": "requester", "dir": "asc"})
    asked = enquiries_client.get(client, "/enquiries?sort=requester&dir=asc")
    assert remembered.shown == asked.shown


def test_a_sort_column_that_is_not_on_the_list_is_ignored(client):
    """The value comes from the URL, so only the whitelist reaches SQL."""
    page = client.get("/enquiries?sort=opp_no;DROP+TABLE+enquiry&dir=asc")
    assert page.status_code == 200



def test_filters_survive_a_sort(client):
    page = enquiries_client.get(client, "/enquiries?view=all&mode=FCL&sort=owner&dir=asc")
    assert "OPP-1401" in page.shown                         # the filter matched
    assert "OPP-1401" not in enquiries_client.get(
        client, "/enquiries?view=all&mode=Air&sort=owner&dir=asc").shown


def test_parties_and_tracking_sort_too(client, app):
    parties = client.get("/parties?sort=key&dir=asc").data.decode("utf-8")
    assert "sort=key" in parties and 'class="sorted"' in parties
    # Tracking v2 sorts in the browser (2026-09-24): ?sort= is ignored, nothing remembered
    tracking = client.get("/tracking?sort=days&dir=asc").data.decode("utf-8")
    assert 'data-sort="days"' in tracking
    conn = conn_of(app)
    assert db.get_setting(conn, "sort_parties") == "key:asc"
    assert db.get_setting(conn, "sort_tracking", "") == ""
    conn.close()
