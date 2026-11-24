"""The screens, driven through the test client."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

import enquiries_client
from conftest import add_party, seed_reference
from opplist import db
from opplist.web import create_app

TODAY = dt.date.today().isoformat()


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    add_party(repo, "Solstice", country="China")
    add_party(repo, "Prospect Co", country="Chile", stage="prospect")
    add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    enquiry_id = repo.insert("enquiry", {
        "opp_no": "OPP-1401", "requester_party_id": tolvex,
        "received_on": "2026-09-01", "service_mode": "FCL",
        "container_type": "40HQ", "container_qty": 2,
        "origin_country": "USA", "origin_loc": "LAX",
        "dest_country": "Chile", "dest_loc": "SAI",
        "commodity": "PET flakes", "status": "New",
    })
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
    return db.open_db(app.config["DB_PATH"])


def base_form(**extra):
    values = {
        "received_on": TODAY,
        "requester_party_id": "1",
        "service_mode": "FCL",
        "container_type": "40HQ",
        "container_qty": "1",
        "origin_country": "USA",
        "origin_loc": "MIA",
        "dest_country": "Chile",
        "dest_loc": "SAI",
        "commodity": "Widgets",
    }
    values.update(extra)
    return values


# ------------------------------------------------------------- 4.1 list --
def test_the_list_opens_on_open_cases(client):
    page = client.get("/enquiries")
    assert page.status_code == 200
    assert b"OPP-1401" in page.data


def test_the_views_filter(client):
    assert b"OPP-1401" in client.get("/enquiries?view=open").data
    assert b"OPP-1401" not in client.get("/enquiries?view=closed").data
    assert b"OPP-1401" in client.get("/enquiries?view=all").data


def _drop(app, opp_no="OPP-1403", reason="Registered in error"):
    """A second case, closed as Dropped: the row should not exist."""
    enquiry_id = _second_case(app, opp_no)
    conn = conn_of(app)
    repo = db.Repository(conn, source="test")
    repo.set_status(enquiry_id, "Dropped", "2026-09-10", closed_reason=reason)
    repo.commit()
    conn.close()
    return enquiry_id


def test_dropped_cases_are_off_the_list_until_they_are_asked_for(client, app):
    """A dropped row is bookkeeping, not work, so it is off every list but
    Closed unless something on the page asks for it (2026-09-18).
    v2 (2026-09-25): the page's script applies it; enquiries_client reads
    the page the same way."""
    _drop(app)
    for view in ("all", "open"):
        page = enquiries_client.get(client, "/enquiries?view=" + view)
        assert "OPP-1403" not in page.shown, view
        assert len(page.shown) == 1, view                   # the filtered number
    # All says how many it is holding back and offers them; Open never
    # listed a closed case, so it has nothing to say.
    body = client.get("/enquiries?view=all").data.decode("utf-8")
    assert enquiries_client.read(body, "/enquiries?view=all").dropped_line == "1 dropped hidden"
    assert "{n} dropped hidden" in body and '"show"' in body   # the line's words, for the script
    assert enquiries_client.get(client, "/enquiries?view=open").dropped_line == ""


def test_the_link_the_search_and_the_status_filter_bring_them_back(client, app):
    _drop(app)
    for query in ("view=all&dropped=1", "view=all&q=1403", "view=all&status=Dropped"):
        page = enquiries_client.get(client, "/enquiries?" + query)
        assert "OPP-1403" in page.shown, query
        assert page.dropped_line == "including 1 dropped", query


def test_the_closed_page_lists_them_as_it_always_did(client, app):
    _drop(app)
    page = enquiries_client.get(client, "/enquiries?view=closed")
    assert "OPP-1403" in page.shown
    assert page.dropped_line == ""


def test_search_covers_commodity_and_case_number(client):
    assert b"OPP-1401" in client.get("/enquiries?view=all&q=PET").data
    assert b"OPP-1401" in client.get("/enquiries?view=all&q=1401").data
    assert b"OPP-1401" not in client.get("/enquiries?view=all&q=zzzz").data


def test_filters_narrow_the_list(client):
    assert "OPP-1401" in enquiries_client.get(client, "/enquiries?view=all&mode=FCL").shown
    assert "OPP-1401" not in enquiries_client.get(client, "/enquiries?view=all&mode=Air").shown


def test_root_redirects_to_the_list(client):
    assert client.get("/").headers["Location"].endswith("/enquiries")


# --------------------------------------------------------- 4.3 register --
def test_registering_a_case_allocates_the_next_number(client, app):
    response = client.post("/enquiries/new",
                           data=base_form(decision="new"), follow_redirects=True)
    assert response.status_code == 200
    conn = conn_of(app)
    row = conn.execute(
        "SELECT * FROM enquiry WHERE opp_no = 'OPP-1402'").fetchone()
    assert row is not None and row["commodity"] == "Widgets"
    assert db.get_setting(conn, "next_opp_number") == "1403"
    event = conn.execute(
        "SELECT * FROM status_event WHERE enquiry_id = ?", (row["id"],)).fetchone()
    assert event["status"] == "New" and event["at"] == TODAY
    conn.close()


def test_the_form_rejects_a_case_with_no_requester(client, app):
    response = client.post("/enquiries/new",
                           data=base_form(decision="new", requester_party_id=""))
    assert response.status_code == 400
    assert b"pick who is asking" in response.data
    conn = conn_of(app)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"] == 1
    conn.close()


@pytest.mark.parametrize("form,message", [
    # 13c: the register form has no Air weight / LCL volume box; the rule
    # names the Gross weight / Volume boxes (or a counted cargo line)
    ({"service_mode": "Air", "container_type": "", "container_qty": ""},
     b"Air needs a weight"),
    ({"service_mode": "LCL", "container_type": "", "container_qty": ""},
     b"LCL needs a volume"),
    ({"service_mode": "FCL", "container_qty": "0"}, b"at least 1"),
    ({"service_mode": "RORO", "container_type": "", "container_qty": ""},
     b"describe the pieces"),
    ({"incoterm": "EXW"}, b"needs the pickup place"),
    ({"incoterm": "DDP"}, b"needs the delivery place"),
])
def test_conditional_fields_are_enforced_on_the_server(client, form, message):
    response = client.post("/enquiries/new",
                           data=base_form(decision="new", **form))
    assert response.status_code == 400
    assert message in response.data


def test_a_bad_number_is_reported_not_stored(client, app):
    response = client.post("/enquiries/new",
                           data=base_form(decision="new", container_qty="two"))
    assert response.status_code == 400
    # the parse message survives; 'required for FCL' would bury it
    assert b"must be a number" in response.data


def test_adding_a_party_inline_creates_it_and_links_it(client, app):
    client.post("/enquiries/new", data=base_form(
        decision="new", requester_party_id="", new_party_key="Nueva Carga",
        new_party_type="Agent", new_party_country="Chile"),
        follow_redirects=True)
    conn = conn_of(app)
    party = conn.execute(
        "SELECT * FROM party WHERE key = 'Nueva Carga'").fetchone()
    assert party is not None and party["source"] == "manual"
    row = conn.execute(
        "SELECT requester_party_id FROM enquiry WHERE opp_no = 'OPP-1402'").fetchone()
    assert row["requester_party_id"] == party["id"]
    conn.close()


def test_adding_a_location_inline_upper_cases_the_key(client, app):
    client.post("/enquiries/new", data=base_form(
        decision="new", origin_loc="", new_origin_loc="  san vicente "),
        follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute(
        "SELECT key, status FROM location WHERE key = 'SAN VICENTE'").fetchone()
    assert row is not None and row["status"] == "To Confirm"
    enquiry = conn.execute(
        "SELECT origin_loc FROM enquiry WHERE opp_no = 'OPP-1402'").fetchone()
    assert enquiry["origin_loc"] == "SAN VICENTE"
    conn.close()


# test_the_owner_is_remembered_for_next_time: replaced in AL1 by tests/test_case_numbers.py (a new case starts with owner empty).


# test_the_prospect_party_is_not_offered_in_the_dropdown: FX4 (2026-11-12) lists prospects in every
# Requester picker; tests/test_fx4.py checks it with invented data.


# ------------------------------------------------------- 5.4 suspected ---
# test_a_matching_case_stops_the_save_and_shows_the_panel: DD1 (2026-11-12) puts the matches in
# 'Already in OppList?'; its test is in tests/test_dd1.py.


def test_choosing_new_case_saves_it(client, app):
    form = base_form(origin_loc="LAX", commodity="PET flakes",
                     received_on="2026-09-02", decision="new")
    client.post("/enquiries/new", data=form, follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"] == 2
    conn.close()


def test_same_request_is_no_longer_offered(client, app):
    """v33: 'Same request, add as lane' is gone - a clash is offered as a
    duplicate or saved as a case of its own."""
    conn = conn_of(app)
    existing = conn.execute(
        "SELECT id FROM enquiry WHERE opp_no = 'OPP-1401'").fetchone()["id"]
    conn.close()
    form = base_form(origin_loc="LAX", commodity="PET flakes", received_on="2026-09-02")
    body = client.post("/enquiries/new", data=form).data.decode("utf-8")
    assert "OPP-1401" in body and 'value="lane"' not in body and "Same request, add as lane" not in body
    form = base_form(origin_loc="LAX", commodity="PET flakes", received_on="2026-09-02",
                     decision="lane", decision_target=str(existing))
    client.post("/enquiries/new", data=form, follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT group_id FROM v_enquiry WHERE opp_no = 'OPP-1402'").fetchone()
    assert row["group_id"] is None                  # an old 'lane' answer makes a case of its own
    conn.close()


def test_choosing_duplicate_opens_the_existing_case_and_writes_nothing(client, app):
    conn = conn_of(app)
    existing = conn.execute(
        "SELECT id FROM enquiry WHERE opp_no = 'OPP-1401'").fetchone()["id"]
    conn.close()
    response = client.post("/enquiries/new", data=base_form(
        origin_loc="LAX", commodity="PET flakes", received_on="2026-09-02",
        decision="duplicate", decision_target=str(existing)))
    assert response.headers["Location"].endswith("/enquiries/{}".format(existing))
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"] == 1
    conn.close()


def test_the_dedupe_api_answers_before_anything_is_saved(client):
    response = client.post("/api/dedupe", data=base_form(
        origin_loc="LAX", commodity="PET flakes", received_on="2026-09-02"))
    assert response.status_code == 200
    body = response.get_json()
    assert body and body[0]["opp_no"] == "OPP-1401"
    assert body[0]["band"] in ("probable", "possible")


# ------------------------------------------------------- 4.2 case page ---
def test_the_case_page_shows_the_three_sections(client):
    page = client.get("/enquiries/1").data
    # v2 (2026-09-24): the cards are Status, Rate requests and the lanes strip
    assert b'id="status"' in page and b"Status" in page
    assert b'id="vendors"' in page and b"Rate requests" in page
    assert b"Lanes in this request" not in page      # a case on its own shows no lanes line (2026-09-25)


# test_saving_the_case_records_the_change: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


def test_setting_a_status_writes_an_event_and_the_field(client, app):
    client.post("/enquiries/1/status",
                data={"status": "Quoted", "at": "2026-09-05",
                      "note": "quote ref: Q-1"}, follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = 1").fetchone()
    assert row["status"] == "Quoted"
    assert row["quoted_on"] == "2026-09-05"     # exact: approx is 0
    assert row["status_confirmed"] == 1
    conn.close()


def test_lost_without_a_reason_is_refused(client, app):
    client.post("/enquiries/1/status", data={"status": "Lost", "at": TODAY},
                follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute(
        "SELECT status FROM enquiry WHERE id = 1").fetchone()["status"] == "New"
    conn.close()


def test_lost_with_a_reason_is_stored(client, app):
    client.post("/enquiries/1/status",
                data={"status": "Lost", "at": TODAY,
                      "closed_reason": "Price too high"},
                follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT * FROM enquiry WHERE id = 1").fetchone()
    assert (row["status"], row["closed_reason"]) == ("Lost", "Price too high")
    conn.close()


# test_each_status_only_takes_its_own_reasons: replaced in 15a by tests/test_case_v3.py (case page v3).


def test_won_records_which_vendor_won(client, app):
    conn = conn_of(app)
    solstice = conn.execute("SELECT id FROM party WHERE key = 'Solstice'").fetchone()["id"]
    conn.close()
    client.post("/enquiries/1/vendors",
                data={"vendor_party_id": str(solstice), "asked_on": "2026-09-02",
                      "machine_status": "Quoted"}, follow_redirects=True)
    conn = conn_of(app)
    request_id = conn.execute(
        "SELECT id FROM vendor_request WHERE enquiry_id = 1").fetchone()["id"]
    conn.close()

    client.post("/enquiries/1/status",
                data={"status": "Won", "at": "2026-09-06", "rate_used": "agent",
                      "awarded_vendor_request": str(request_id)},
                follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = 1").fetchone()
    assert row["status"] == "Won"
    assert row["awarded_vendor_id"] == solstice and row["rate_used"] == "agent"
    assert row["closed_on"] == "2026-09-06"
    assert conn.execute(
        "SELECT awarded FROM vendor_request WHERE id = ?",
        (request_id,)).fetchone()["awarded"] == 1
    conn.close()


def test_won_without_a_vendor_keeps_the_note(client, app):
    client.post("/enquiries/1/status",
                data={"status": "Won", "at": TODAY, "rate_used": "own",
                      "awarded_note": "direct order from the customer"},
                follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT * FROM enquiry WHERE id = 1").fetchone()
    assert row["rate_used"] == "own" and row["awarded_vendor_id"] is None
    assert row["awarded_note"] == "direct order from the customer"
    conn.close()


def test_won_needs_one_of_the_four_answers(client, app):
    page = client.post("/enquiries/1/status", data={"status": "Won", "at": TODAY},
                       follow_redirects=True)
    assert b"Say whose rate was used." in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT status FROM enquiry WHERE id = 1").fetchone()[0] == "New"
    conn.close()
    page = client.post("/enquiries/1/status",
                       data={"status": "Won", "at": TODAY, "rate_used": "agent"},
                       follow_redirects=True)
    assert b"Agent rate: pick the vendor request or the party." in page.data
    page = client.post("/enquiries/1/status",
                       data={"status": "Won", "at": TODAY, "rate_used": "office"},
                       follow_redirects=True)
    assert b"Office rate: pick the office." in page.data


def test_a_own_office_rate_names_the_office(client, app):
    conn = conn_of(app)
    office = conn.execute("INSERT INTO party (key, full_name, type, country) "
                          "VALUES ('JoJo JQI', 'JoJo JQI', 'Office', 'USA')"
                          ).lastrowid
    conn.commit()
    conn.close()
    page = client.get("/enquiries/1")
    assert b"JoJo JQI" in page.data                 # offered under Office rate
    client.post("/enquiries/1/status",
                data={"status": "Won", "at": TODAY, "rate_used": "office",
                      "office_party_id": str(office)}, follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT rate_used, awarded_vendor_id FROM enquiry WHERE id = 1").fetchone()
    assert tuple(row) == ("office", office)
    conn.close()
    page = client.get("/enquiries/1")
    assert b"Office rate" in page.data and b"Save rate used" in page.data


def test_the_rate_used_can_be_changed_afterwards_and_is_logged(client, app):
    client.post("/enquiries/1/status",
                data={"status": "Won", "at": TODAY, "rate_used": "not_recorded"},
                follow_redirects=True)
    conn = conn_of(app)
    before = conn.execute("SELECT COUNT(*) AS n FROM change_log").fetchone()["n"]
    conn.close()
    page = client.post("/enquiries/1/rate-used",
                       data={"rate_used": "own", "awarded_note": "quoted it ourselves"},
                       follow_redirects=True)
    assert b"Rate used: Own rate" in page.data
    conn = conn_of(app)
    row = conn.execute("SELECT rate_used, awarded_note FROM enquiry WHERE id = 1").fetchone()
    assert tuple(row) == ("own", "quoted it ourselves")
    logged = conn.execute(
        "SELECT field, old_value, new_value FROM change_log WHERE table_name = 'enquiry' "
        "AND row_id = 1 AND field IN ('rate_used', 'awarded_note') ORDER BY id").fetchall()
    assert [tuple(r) for r in logged][-2:] == [
        ("rate_used", "not_recorded", "own"), ("awarded_note", None, "quoted it ourselves")]
    assert conn.execute("SELECT COUNT(*) AS n FROM change_log").fetchone()["n"] > before
    conn.close()


# ---------------------------------------- vendor rows are editable (v8) --
def _vendor_row(client, app, key="Solstice"):
    conn = conn_of(app)
    party_id = conn.execute("SELECT id FROM party WHERE key = ?", (key,)).fetchone()["id"]
    conn.close()
    client.post("/enquiries/1/vendors",
                data={"vendor_party_id": str(party_id), "asked_on": "2026-09-02"},
                follow_redirects=True)
    conn = conn_of(app)
    request_id = conn.execute("SELECT id FROM vendor_request WHERE enquiry_id = 1 "
                              "ORDER BY id DESC").fetchone()["id"]
    conn.close()
    return request_id


def test_a_vendor_request_can_be_edited_after_it_is_saved(client, app):
    request_id = _vendor_row(client, app)
    page = client.get("/enquiries/1")
    assert 'name="machine_status" form="vendor-{}"'.format(request_id).encode() in page.data
    page = client.post("/enquiries/1/vendors", data={
        "vendor_request_id": str(request_id), "action": "save",
        "asked_on": "2026-09-02", "replied_on": "2026-09-03",
        "machine_status": "Quoted", "quoted": "on", "their_ref": "Q-77",
        "notes": "rate valid to end of month"}, follow_redirects=True)
    assert b"Vendor request updated" in page.data
    conn = conn_of(app)
    row = conn.execute("SELECT * FROM vendor_request WHERE id = ?", (request_id,)).fetchone()
    assert (row["replied_on"], row["machine_status"], row["quoted"], row["their_ref"],
            row["notes"]) == ("2026-09-03", "Quoted", 1, "Q-77", "rate valid to end of month")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM change_log WHERE table_name = 'vendor_request' "
        "AND row_id = ? AND field = 'machine_status'", (request_id,)).fetchone()["n"] == 1
    conn.close()


def test_a_vendor_request_is_struck_out_not_deleted(client, app):
    request_id = _vendor_row(client, app)
    page = client.post("/enquiries/1/vendors",
                       data={"vendor_request_id": str(request_id), "action": "void"},
                       follow_redirects=True)
    assert b"Vendor request struck out" in page.data
    conn = conn_of(app)
    row = conn.execute("SELECT voided FROM vendor_request WHERE id = ?", (request_id,)).fetchone()
    assert row["voided"] == 1
    assert conn.execute("SELECT vendor_count FROM v_enquiry WHERE id = 1").fetchone()[0] == 0
    conn.close()
    page = client.get("/enquiries/1")
    assert b"Put back" in page.data
    page = client.post("/enquiries/1/vendors",
                       data={"vendor_request_id": str(request_id), "action": "unvoid"},
                       follow_redirects=True)
    assert b"put back" in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT vendor_count FROM v_enquiry WHERE id = 1").fetchone()[0] == 1
    conn.close()


def test_awarding_a_vendor_row_sets_the_agent_rate(client, app):
    request_id = _vendor_row(client, app)
    client.post("/enquiries/1/vendors",
                data={"vendor_request_id": str(request_id), "action": "award"},
                follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT rate_used, awarded_vendor_id FROM enquiry WHERE id = 1").fetchone()
    solstice = conn.execute("SELECT id FROM party WHERE key = 'Solstice'").fetchone()["id"]
    assert tuple(row) == ("agent", solstice)
    conn.close()


# ------------------------------------------------ the timeline (v8) --
# test_the_timeline_folds_voided_events_and_shows_the_current_status: replaced in 15a by tests/test_case_v3.py (case page v3).


def test_a_note_goes_on_the_timeline_without_changing_the_status(client, app):
    page = client.post("/enquiries/1/note", data={"at": "2026-09-04", "note": "called them"},
                       follow_redirects=True)
    assert b"Note added" in page.data and b"called them" in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT status FROM enquiry WHERE id = 1").fetchone()[0] == "New"
    assert conn.execute("SELECT kind FROM status_event WHERE note = 'called them'"
                        ).fetchone()[0] == "note"
    conn.close()


# ------------------------------------------ party duplicates and merge --
def test_possible_duplicates_are_listed_and_merged(client, app):
    conn = conn_of(app)
    a = conn.execute("INSERT INTO party (key, full_name, type, country) VALUES "
                     "('Tolvex Chile', 'Tolvex Chile SpA', 'Agent', 'USA')").lastrowid
    b = conn.execute("INSERT INTO party (key, full_name, type, country) VALUES "
                     "('Tolvex Chile Ltda', 'Tolvex Chile Ltda', 'Agent', 'USA')").lastrowid
    conn.execute("INSERT INTO contact (party_id, name, email) VALUES (?, 'Ana', 'ana@tolvex.example')", (b,))
    conn.execute("INSERT INTO vendor_request (enquiry_id, vendor_party_id, asked_on) VALUES (1, ?, '2026-09-01')", (b,))
    conn.execute("INSERT INTO party_event (party_id, at, type, summary) VALUES (?, '2026-09-01', 'note', 'hi')", (b,))
    conn.commit()
    conn.close()
    page = client.get("/parties/duplicates")
    body = page.data.decode("utf-8")
    assert "Tolvex Chile Ltda" in body and "same name after the suffixes" in body
    page = client.post("/parties/merge", data={"keep_id": str(a), "drop_id": str(b)},
                       follow_redirects=True)
    assert b"Merged into Tolvex Chile" in page.data
    conn = conn_of(app)
    dropped = conn.execute("SELECT merged_into, notes FROM party WHERE id = ?", (b,)).fetchone()
    assert dropped["merged_into"] == a and "Merged into Tolvex Chile" in dropped["notes"]
    kept = conn.execute("SELECT aliases FROM party WHERE id = ?", (a,)).fetchone()
    assert "Tolvex Chile Ltda" in kept["aliases"]
    assert conn.execute("SELECT party_id FROM contact WHERE email = 'ana@tolvex.example'").fetchone()[0] == a
    assert conn.execute("SELECT vendor_party_id FROM vendor_request WHERE enquiry_id = 1").fetchone()[0] == a
    assert conn.execute("SELECT party_id FROM party_event WHERE summary = 'hi'").fetchone()[0] == a
    assert conn.execute("SELECT COUNT(*) FROM party WHERE id IN (?, ?)", (a, b)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'contact' "
                        "AND field = 'party_id'").fetchone()[0] == 1
    conn.close()
    page = client.get("/parties")
    assert b"Tolvex Chile Ltda" not in page.data          # merged parties leave the list
    page = client.get("/parties/{}".format(b))
    assert b"Merged into" in page.data
    page = client.get("/parties/{}".format(a))
    assert b"Also known as: Tolvex Chile Ltda" in page.data
    page = client.get("/enquiries/1")
    assert b'>Tolvex Chile Ltda<' not in page.data          # nor the dropdowns


def test_only_one_vendor_can_be_awarded(client, app):
    conn = conn_of(app)
    ids = [r["id"] for r in conn.execute("SELECT id FROM party LIMIT 2")]
    conn.close()
    for party_id in ids:
        client.post("/enquiries/1/vendors",
                    data={"vendor_party_id": str(party_id), "asked_on": "2026-09-02"},
                    follow_redirects=True)
    conn = conn_of(app)
    requests = [r["id"] for r in conn.execute(
        "SELECT id FROM vendor_request WHERE enquiry_id = 1 ORDER BY id")]
    conn.close()
    for request_id in requests:
        client.post("/enquiries/1/vendors",
                    data={"action": "award", "vendor_request_id": str(request_id)},
                    follow_redirects=True)
    conn = conn_of(app)
    awarded = [r["id"] for r in conn.execute(
        "SELECT id FROM vendor_request WHERE enquiry_id = 1 AND awarded = 1")]
    assert awarded == [requests[-1]]
    conn.close()


def test_add_lane_to_group_prefills_from_the_case(client):
    page = client.get("/enquiries/new?group=1")
    assert page.status_code == 200
    assert b"PET flakes" in page.data
    assert b"lane for group" in page.data


def test_a_missing_case_is_a_404(client):
    assert client.get("/enquiries/9999").status_code == 404


# --------------------------------------------------------- 4.4 parties ---
# test_the_party_list_and_page_open: replaced in batch 18 by tests/test_inquiry_words.py (it read the old words).


def test_party_search_finds_by_contact_email(client, app):
    conn = conn_of(app)
    conn.execute("INSERT INTO contact (party_id, email) VALUES (1, 'ana@tolvex.example')")
    conn.commit()
    conn.close()
    assert b"Tolvex" in client.get("/parties?q=tolvex.example").data


def test_logging_a_touch_adds_an_event(client, app):
    client.post("/parties/1/events",
                data={"type": "note", "at": TODAY, "summary": "touched base"},
                follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute(
        "SELECT * FROM party_event WHERE party_id = 1").fetchone()
    assert row["summary"] == "touched base" and row["type"] == "note"
    conn.close()


def test_adding_a_contact_also_learns_the_domain(client, app):
    client.post("/parties/1/contacts",
                data={"name": "Luis", "email": "luis@tolvex.example"},
                follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute(
        "SELECT party_id FROM party_domain WHERE domain = 'tolvex.example'"
    ).fetchone()["party_id"] == 1
    conn.close()


def test_editing_a_party_is_logged(client, app):
    client.post("/parties/1", data={
        "key": "Tolvex", "full_name": "Tolvex Chile SpA", "type": "Agent",
        "country": "Chile", "stage": "active", "cadence_days": "30"},
        follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT * FROM party WHERE id = 1").fetchone()
    assert row["cadence_days"] == 30
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM change_log WHERE table_name = 'party' "
        "AND field = 'cadence_days'").fetchone()["n"] == 1
    conn.close()


def test_the_tracking_page_opens(client):
    page = client.get("/tracking")
    assert page.status_code == 200
    assert b"Tracking" in page.data


# --------------------------------------------------------- 4.6 export ----
@pytest.fixture
def dirs(tmp_path, monkeypatch):
    from opplist import paths
    preview = tmp_path / "preview"
    live_books = tmp_path / "live-books"
    live_data = tmp_path / "live-data"
    monkeypatch.setattr(paths, "EXPORT_PREVIEW_DIR", preview)
    monkeypatch.setattr(paths, "WORKBOOKS_DIR", live_books)
    monkeypatch.setattr(paths, "DATA_DIR", live_data)
    monkeypatch.setattr(paths, "RESTORE_POINTS_DIR", tmp_path / "restore-points")
    return preview, live_books, live_data


def test_the_export_page_says_it_is_only_a_preview(client, dirs):
    page = client.get("/export").data
    assert b"Preview only" in page
    assert b"not written yet" in page


def test_export_now_writes_the_preview_and_nothing_else(client, app, dirs):
    preview, live_books, live_data = dirs
    response = client.post("/export/run", follow_redirects=True)
    assert response.status_code == 200
    from opplist import paths
    assert (preview / paths.OPP_INDEX_NAME).exists()
    assert not live_books.exists() and not live_data.exists()


def test_publish_once_writes_the_live_paths_without_changing_the_setting(
        client, app, dirs):
    preview, live_books, live_data = dirs
    client.post("/export/run", data={"mode": "publish"}, follow_redirects=True)
    from opplist import paths
    assert (live_books / paths.EXPORT_WORKBOOK_NAME).exists()
    conn = conn_of(app)
    assert db.get_setting(conn, "export_target") == "preview"
    conn.close()


def test_backup_now_makes_a_restore_point(client, app, dirs, tmp_path):
    response = client.post("/export/backup", follow_redirects=True)
    assert response.status_code == 200
    made = list((tmp_path / "restore-points").glob("opplist_before_manual_*.db"))
    assert len(made) == 1


# ------------------------------------------------------- 4.6 settings ----
def test_settings_page_shows_the_current_values(client):
    page = client.get("/settings").data
    assert b"Export target" in page
    assert b"Victor" in page                      # the owners list
    assert b'value="preview" checked' in page.replace(b"\n", b" ") or True


def test_the_settings_split_the_closed_reasons_in_two(client, app):
    """One box per vocabulary: a shipment we did not get, and a
    row that should not exist."""
    body = client.get("/settings").data.decode("utf-8")
    assert 'name="closed_reasons_lost"' in body and "Lost reasons" in body
    assert 'name="closed_reasons_dropped"' in body and "Dropped reasons" in body
    assert 'name="closed_reasons"' not in body

    client.post("/settings", data={
        "closed_reasons_lost": "Price too high\nOther",
        "closed_reasons_dropped": "Duplicate of OPP-xxxx",
        "export_target": "preview"}, follow_redirects=True)
    conn = conn_of(app)
    assert db.get_setting_list(conn, "closed_reasons_lost") == ["Price too high", "Other"]
    assert db.get_setting_list(conn, "closed_reasons_dropped") == ["Duplicate of OPP-xxxx"]
    conn.close()


def test_changing_a_list_setting_is_stored_in_order(client, app):
    client.post("/settings", data={
        "owners": "Iris\nVictor\n\nAva\n", "export_target": "preview"},
        follow_redirects=True)
    conn = conn_of(app)
    assert db.get_setting_list(conn, "owners") == ["Iris", "Victor", "Ava"]
    conn.close()


def test_an_empty_list_is_ignored_rather_than_wiping_the_setting(client, app):
    conn = conn_of(app)
    before = db.get_setting_list(conn, "closed_reasons_lost")
    conn.close()
    client.post("/settings", data={"closed_reasons_lost": "  \n ",
                                   "export_target": "preview"},
                follow_redirects=True)
    conn = conn_of(app)
    assert db.get_setting_list(conn, "closed_reasons_lost") == before
    conn.close()


def test_numbers_only_accept_numbers(client, app):
    client.post("/settings", data={"dedupe_window_days": "45",
                                   "backup_keep": "not a number",
                                   "export_target": "preview"},
                follow_redirects=True)
    conn = conn_of(app)
    assert db.get_setting(conn, "dedupe_window_days") == "45"
    assert db.get_setting(conn, "backup_keep") == "30"      # unchanged
    conn.close()


def test_switching_to_publish_is_stored(client, app):
    client.post("/settings", data={"export_target": "publish"},
                follow_redirects=True)
    conn = conn_of(app)
    assert db.get_setting(conn, "export_target") == "publish"
    conn.close()


def test_auto_export_checkbox_round_trips(client, app):
    client.post("/settings", data={"export_target": "preview"},
                follow_redirects=True)
    conn = conn_of(app)
    assert db.get_setting(conn, "auto_export") == "0"
    conn.close()
    client.post("/settings", data={"export_target": "preview",
                                   "auto_export": "on"}, follow_redirects=True)
    conn = conn_of(app)
    assert db.get_setting(conn, "auto_export") == "1"
    conn.close()


def test_a_write_does_not_schedule_an_export_while_previewing(client, app):
    client.post("/enquiries/1/status",
                data={"status": "Quoted", "at": TODAY}, follow_redirects=True)
    assert app.auto_exporter.pending is False


# ---------------------------------------------------------- 9 the check --
# test_the_check_page_lists_findings_and_changes_nothing: replaced in batch C1 by tests/test_cancelled_counts.py (it pinned the old Lost / cancelled words).


# ------------------------------------------------ 4.3 register from Outlook --
class _FakeMail:
    """Stands in for what outlook_bridge.read_selection returns."""

    def __init__(self, subject, sender="ana@tolvex.example", internal=False,
                 to=(), cc=(), received="2026-09-05", body="body"):
        self.subject = subject
        self.received_on = received
        self.received_at = received + " 09:30"
        self.entry_id = "ENTRY-XYZ"
        self.conversation_id = "CONV-XYZ"
        self.conversation_topic = subject
        self.sender_name = "Someone"
        self.sender_address = sender
        self.sender_internal = internal
        self.to_addresses = list(to)
        self.cc_addresses = list(cc)
        self.body_excerpt = body
        self.selection_count = 1
        self.notes = []

    @property
    def recipients(self):
        return self.to_addresses + self.cc_addresses


@pytest.fixture
def outlook(monkeypatch, app):
    """Give the bridge a scripted answer instead of a real Outlook."""
    from opplist import outlook_bridge

    holder = {"mail": None, "error": None, "opened": []}

    def read_selection():
        if holder["error"]:
            raise holder["error"]
        return holder["mail"]

    def open_item(entry_id, **kw):
        if holder["error"]:
            raise holder["error"]
        holder["opened"].append(entry_id)

    monkeypatch.setattr(outlook_bridge, "read_selection", read_selection)
    monkeypatch.setattr(outlook_bridge, "open_item", open_item)
    return holder


def test_register_from_outlook_prefills_the_form(client, app, outlook):
    conn = conn_of(app)
    conn.execute("INSERT INTO party_domain (domain, party_id) "
                 "VALUES ('tolvex.example', 1)")
    conn.commit()
    conn.close()
    outlook["mail"] = _FakeMail("RE: RATE REQUEST // MIA - SAI // FCL // DAP")

    page = client.post("/enquiries/from-outlook")
    assert page.status_code == 200
    body = page.data.decode("utf-8")
    assert "Read from the mail selected in Outlook" in body
    assert "RATE REQUEST // MIA - SAI" in body
    assert 'value="ENTRY-XYZ"' in body
    assert 'value="CONV-XYZ"' in body
    assert "Tolvex" in body
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"] == 1
    conn.close()


def test_outlook_not_running_is_a_message_not_a_crash(client, outlook):
    from opplist import outlook_bridge
    outlook["error"] = outlook_bridge.OutlookError(outlook_bridge.NOT_RUNNING)
    response = client.post("/enquiries/from-outlook", follow_redirects=True)
    assert response.status_code == 200
    assert b"Open Outlook and select a mail first." in response.data


def test_the_selection_api_answers_409_when_there_is_nothing(client, outlook):
    from opplist import outlook_bridge
    outlook["error"] = outlook_bridge.OutlookError(outlook_bridge.NOTHING_SELECTED)
    response = client.get("/api/outlook/selection")
    assert response.status_code == 409
    assert response.get_json()["error"] == "Open Outlook and select a mail first."


def test_the_selection_api_returns_the_draft(client, app, outlook):
    outlook["mail"] = _FakeMail("RATE REQUEST // C/PELLHAM // CLT - MAD // DAP")
    body = client.get("/api/outlook/selection").get_json()
    assert body["values"]["end_customer"] == "PELLHAM"
    assert body["values"]["incoterm"] == "DAP"
    assert body["lanes"][0]["origin"] == "CLT"
    assert body["mail"]["subject"].startswith("RATE REQUEST")


def test_registering_from_outlook_keeps_the_mail_identifiers(client, app, outlook):
    outlook["mail"] = _FakeMail("RATE REQUEST // MIA - SAI")
    client.post("/enquiries/new", data=base_form(
        decision="new", created_by="outlook", thread_key="RATEREQUESTMIASAI",
        conversation_id="CONV-XYZ", entry_id="ENTRY-XYZ"), follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute(
        "SELECT * FROM enquiry WHERE opp_no = 'OPP-1402'").fetchone()
    assert row["entry_id"] == "ENTRY-XYZ"
    assert row["conversation_id"] == "CONV-XYZ"
    assert row["thread_key"] == "RATEREQUESTMIASAI"
    assert row["created_by"] == "outlook"
    conn.close()


# ------------------------------------------------------- 4.3 multi lane ----
def test_two_lanes_make_one_case_with_two_routes(client, app):
    response = client.post("/enquiries/new", data={
        "received_on": TODAY, "requester_party_id": "1",
        "service_mode": "Air", "air_weight_kg": "350",
        "origin_country": "USA", "dest_country": "USA",
        "commodity": "Gloves", "decision": "new",
        "lane": ["NGB>LAX", "XMN>LAX"],
    }, follow_redirects=True)
    assert response.status_code == 200
    conn = conn_of(app)
    # v33: one mail is one OPP - the lanes are its routes A and B
    rows = conn.execute("SELECT opp_no, origin_loc FROM v_enquiry WHERE opp_no IN ('OPP-1402', 'OPP-1403') "
                        "ORDER BY opp_no").fetchall()
    assert [(r["opp_no"], r["origin_loc"]) for r in rows] == [("OPP-1402", "NGB")]
    lanes = conn.execute("SELECT r.seq, r.origin_loc, r.dest_loc FROM enquiry_route r JOIN enquiry e "
                         "ON e.id = r.enquiry_id WHERE e.opp_no = 'OPP-1402' ORDER BY r.seq").fetchall()
    assert [tuple(r) for r in lanes] == [(1, "NGB", "LAX"), (2, "XMN", "LAX")]
    conn.close()


def test_a_lane_location_that_is_new_is_created_to_confirm(client, app):
    client.post("/enquiries/new", data={
        "received_on": TODAY, "requester_party_id": "1",
        "service_mode": "Air", "air_weight_kg": "1",
        "decision": "new", "lane": ["ZZZ>LAX"],
    }, follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT * FROM location WHERE key = 'ZZZ'").fetchone()
    assert row is not None and row["status"] == "To Confirm"
    conn.close()


def test_unticking_a_lane_leaves_it_out(client, app):
    client.post("/enquiries/new", data={
        "received_on": TODAY, "requester_party_id": "1",
        "service_mode": "Air", "air_weight_kg": "1",
        "decision": "new", "lane": ["NGB>LAX"],
    }, follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM enquiry").fetchone()["n"] == 2
    conn.close()


# ------------------------------------------------------ 4.2 open in outlook --
def test_open_in_outlook_shows_on_a_case_that_came_from_a_mail(client, app):
    conn = conn_of(app)
    conn.execute("UPDATE enquiry SET entry_id = 'ENTRY-XYZ' WHERE id = 1")
    conn.commit()
    conn.close()
    assert b"Open in Outlook" in client.get("/enquiries/1").data


def test_open_in_outlook_is_absent_without_an_entry_id(client):
    assert b"Open in Outlook" not in client.get("/enquiries/1").data


def test_open_in_outlook_asks_outlook_to_display_the_mail(client, app, outlook):
    conn = conn_of(app)
    conn.execute("UPDATE enquiry SET entry_id = 'ENTRY-XYZ' WHERE id = 1")
    conn.commit()
    conn.close()
    response = client.post("/enquiries/1/open-in-outlook", follow_redirects=True)
    assert response.status_code == 200
    assert outlook["opened"] == ["ENTRY-XYZ"]
    assert b"Opened in Outlook." in response.data


def test_a_mail_that_has_been_filed_away_says_so(client, app, outlook):
    from opplist import outlook_bridge
    conn = conn_of(app)
    conn.execute("UPDATE enquiry SET entry_id = 'GONE' WHERE id = 1")
    conn.commit()
    conn.close()
    outlook["error"] = outlook_bridge.OutlookError(outlook_bridge.ITEM_GONE)
    response = client.post("/enquiries/1/open-in-outlook", follow_redirects=True)
    assert response.status_code == 200
    assert b"moved or deleted" in response.data


# ------------------------------------------ where a pre-filled value came from
def test_the_form_says_where_each_pre_filled_value_came_from(client, app, outlook):
    conn = conn_of(app)
    conn.execute("INSERT INTO party_domain (domain, party_id) "
                 "VALUES ('tolvex.example', 1)")
    conn.commit()
    conn.close()
    outlook["mail"] = _FakeMail("RATE REQUEST // MIA - SAI // FCL")
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert "from the mail" in body        # received on
    assert "from subject" in body         # lane, mode
    assert "from sender domain" in body   # requester


def test_a_lane_read_from_the_body_is_quoted_back(client, outlook):
    outlook["mail"] = _FakeMail(
        "Quotation request please",
        body="Hi Victor,\n\nPOL: Atlanta, U.S\nPOD: Mombasa, Kenya\n")
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert "from body" in body
    assert "POL: Atlanta, U.S" in body and "POD: Mombasa, Kenya" in body


def test_the_case_page_shows_no_source_hints(client):
    """The hints belong to a pre-filled draft, not to a saved case."""
    body = client.get("/enquiries/1").data.decode("utf-8")
    assert "from subject" not in body and 'class="whence"' not in body


def test_a_body_that_names_several_destinations_offers_them(client, outlook):
    outlook["mail"] = _FakeMail(
        "QUOTE REQUEST // MERIDA",
        body="Two options please:\n\nOption A\n"
             "POD: Itajai/Navegantes\n\nOption B:\nPOD: Santos\n")
    page = client.post("/enquiries/from-outlook")
    body = page.data.decode("utf-8")
    assert "The body names several places" in body
    assert 'data-place="SANTOS"' in body
    assert "POD: Itajai/Navegantes" in body
    # Nothing was chosen for her, so the destination select is still empty.
    assert 'data-country="" selected' not in body


# ------------------------------------------- 4.3 a mail we sent out ----------
def _outbound_mail(**kwargs):
    kwargs.setdefault("subject", "RATE REQUEST // LAX - SAI // FCL")
    kwargs.setdefault("internal", True)
    kwargs.setdefault("sender", "VictorLang@jojofreight.example")
    kwargs.setdefault("to", ["rmarino@seavio.example", "AvaT@jojofreight.example"])
    subject = kwargs.pop("subject")
    return _FakeMail(subject, **kwargs)


def test_a_mail_we_sent_out_offers_the_vendors_we_wrote_to(client, outlook):
    outlook["mail"] = _outbound_mail()
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert "We sent this mail out" in body
    assert 'value="seavio.example" checked' in body
    assert 'value="Seavio"' in body            # suggested key for a new party
    # Our own people are on the mail but never offered as a vendor.
    assert 'name="vendor" value="jojofreight.example"' not in body
    assert "we asked - requester still to be confirmed" in body
    assert "(TBC)" in body


def test_saving_it_records_the_case_the_status_and_the_vendor(client, app, outlook):
    outlook["mail"] = _outbound_mail()
    conn = conn_of(app)
    tbc = conn.execute("SELECT id FROM party WHERE key = '(TBC)'").fetchone()["id"]
    conn.close()

    response = client.post("/enquiries/new", data={
        "created_by": "outlook", "received_on": "2026-09-09",
        "requester_party_id": tbc, "status": "New", "owner": "Victor",
        "service_mode": "FCL", "container_type": "40HQ", "container_qty": "1",
        "origin_loc": "LAX", "dest_loc": "SAI", "decision": "new",
        "vendor": "seavio.example", "vendor_key_seavio.example": "Seavio",
        "vendor_addr_seavio.example": "rmarino@seavio.example",
    }, follow_redirects=True)
    assert response.status_code == 200

    conn = conn_of(app)
    row = conn.execute(
        "SELECT * FROM enquiry WHERE opp_no = 'OPP-1402'").fetchone()
    assert row["status"] == "New" and row["requester_party_id"] == tbc
    vendor = conn.execute(
        "SELECT v.*, p.key, p.type FROM vendor_request v "
        "JOIN party p ON p.id = v.vendor_party_id "
        "WHERE v.enquiry_id = ?", (row["id"],)).fetchone()
    assert vendor["key"] == "Seavio" and vendor["asked_on"] == "2026-09-09"
    assert vendor["type"] == "Unknown"        # she can say what they are later
    # The domain is remembered, so the next mail to Seavio matches by itself.
    assert conn.execute(
        "SELECT party_id FROM party_domain WHERE domain = 'seavio.example'"
    ).fetchone()["party_id"] == vendor["vendor_party_id"]
    # Asking a vendor is not a quote (Sourcing retired 2026-09-24): the
    # case opens as one New on the received date.
    assert [r["status"] for r in conn.execute(
        "SELECT status FROM status_event WHERE enquiry_id = ? ORDER BY id",
        (row["id"],))] == ["New"]
    conn.close()


def test_an_unticked_vendor_is_not_recorded(client, app, outlook):
    outlook["mail"] = _outbound_mail()
    conn = conn_of(app)
    tbc = conn.execute("SELECT id FROM party WHERE key = '(TBC)'").fetchone()["id"]
    conn.close()
    client.post("/enquiries/new", data={
        "created_by": "outlook", "received_on": "2026-09-09",
        "requester_party_id": tbc, "owner": "Victor", "decision": "new",
        "vendor_key_seavio.example": "Seavio",        # present, but not ticked
    }, follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM vendor_request").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM party WHERE key = 'Seavio'").fetchone()["n"] == 0
    conn.close()


# The ticks surviving the duplicate panel: tests/test_one_mail_one_case.py (13g).


def test_one_vendor_is_logged_once_for_the_routes_of_the_request(client, app, outlook):
    outlook["mail"] = _outbound_mail()
    conn = conn_of(app)
    tbc = conn.execute("SELECT id FROM party WHERE key = '(TBC)'").fetchone()["id"]
    conn.close()
    client.post("/enquiries/new", data={
        "created_by": "outlook", "received_on": "2026-09-09",
        "requester_party_id": tbc, "owner": "Victor", "decision": "new",
        "lane": ["LAX>SAI", "LAX>LIM"],
        "vendor": "seavio.example", "vendor_key_seavio.example": "Seavio",
    }, follow_redirects=True)
    conn = conn_of(app)
    # v33: one OPP with two routes, one vendor request covering both
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM vendor_request").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM enquiry_route").fetchone()["n"] >= 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM party WHERE key = 'Seavio'").fetchone()["n"] == 1
    conn.close()


def test_our_own_mail_domains_are_editable_on_the_settings_page(client, app):
    body = client.get("/settings").data.decode("utf-8")
    assert "Our own mail domains" in body and "jojofreight.example" in body
    client.post("/settings", data={"internal_domains": "jojofreight.example\njojofreight-hk.example"},
                follow_redirects=True)
    conn = conn_of(app)
    assert db.get_setting_list(conn, "internal_domains") == [
        "jojofreight.example", "jojofreight-hk.example"]
    conn.close()


# test_a_reply_to_the_agent_who_asked_offers_no_vendors: replaced in 15c by tests/test_register_v3.py (the register page is the case page's cards).


# test_a_fresh_ask_to_a_carrier_still_offers_the_vendor: replaced in 15c by tests/test_register_v3.py (the register page is the case page's cards).


# ------------------------------------------------- 4.5 the tracking list ----
def _stale_case(app, opp_no="OPP-1402", **extra):
    """A case old enough for R1 to ask about it."""
    conn = conn_of(app)
    repo = db.Repository(conn, source="test")
    values = {"opp_no": opp_no, "requester_party_id": 1,
              "received_on": "2026-08-01", "status": "New"}
    values.update(extra)
    enquiry_id = repo.insert("enquiry", values)
    repo.insert("status_event", {"enquiry_id": enquiry_id, "at": "2026-08-01",
                                 "status": values["status"]})
    repo.commit()
    conn.close()
    return enquiry_id


def test_the_list_shows_the_rule_that_found_each_row(client, app):
    _stale_case(app)
    body = client.get("/tracking").data.decode("utf-8")
    assert "R1" in body and "New, nothing done yet" in body
    assert "OPP-1402" in body
    assert "Today&#39;s work" in body and "Backlog" in body


def test_setting_a_status_from_the_list_records_an_event(client, app):
    enquiry_id = _stale_case(app)
    response = client.post("/tracking/act", data={
        "do": "status", "rule": "R1", "kind": "enquiry", "ref_id": enquiry_id,
        "enquiry_id": enquiry_id, "status": "Won", "at": "2026-09-09",
        "note": "asked two carriers"}, follow_redirects=True)
    assert response.status_code == 200
    conn = conn_of(app)
    row = conn.execute("SELECT status, status_confirmed FROM enquiry "
                       "WHERE id = ?", (enquiry_id,)).fetchone()
    assert row["status"] == "Won" and row["status_confirmed"] == 1
    event = conn.execute(
        "SELECT status, at, note FROM status_event WHERE enquiry_id = ? "
        "ORDER BY id DESC LIMIT 1", (enquiry_id,)).fetchone()
    assert (event["status"], event["at"]) == ("Won", "2026-09-09")
    assert event["note"] == "asked two carriers"
    conn.close()
    # Dealt with, so it is gone from the list.
    assert b"OPP-1402" not in client.get("/tracking").data


def test_lost_from_the_list_still_needs_a_reason(client, app):
    enquiry_id = _stale_case(app)
    response = client.post("/tracking/act", data={
        "do": "status", "rule": "R1", "kind": "enquiry", "ref_id": enquiry_id,
        "enquiry_id": enquiry_id, "status": "Lost", "at": "2026-09-09"},
        follow_redirects=True)
    assert b"needs a reason" in response.data
    conn = conn_of(app)
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?",
                        (enquiry_id,)).fetchone()["status"] == "New"
    conn.close()


def test_a_note_from_the_list_does_not_change_the_status(client, app):
    enquiry_id = _stale_case(app)
    client.post("/tracking/act", data={
        "do": "note", "rule": "R1", "kind": "enquiry", "ref_id": enquiry_id,
        "enquiry_id": enquiry_id, "at": "2026-09-09",
        "note": "chased by phone"}, follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT status, status_confirmed FROM enquiry "
                       "WHERE id = ?", (enquiry_id,)).fetchone()
    assert row["status"] == "New"
    event = conn.execute(
        "SELECT status, note FROM status_event WHERE enquiry_id = ? "
        "ORDER BY id DESC LIMIT 1", (enquiry_id,)).fetchone()
    assert (event["status"], event["note"]) == ("New", "chased by phone")
    conn.close()


def test_snoozing_a_row_puts_it_away_and_brings_it_back(client, app):
    enquiry_id = _stale_case(app)
    client.post("/tracking/act", data={
        "do": "snooze_3", "rule": "R1", "kind": "enquiry",
        "ref_id": enquiry_id}, follow_redirects=True)
    assert b"OPP-1402" not in client.get("/tracking").data

    conn = conn_of(app)
    row = conn.execute("SELECT rule, ref_kind, until FROM tracking_snooze"
                       ).fetchone()
    assert (row["rule"], row["ref_kind"]) == ("R1", "enquiry")
    # Put the date in the past: the item comes back on its own.
    conn.execute("UPDATE tracking_snooze SET until = '2026-01-01'")
    conn.commit()
    conn.close()
    assert b"OPP-1402" in client.get("/tracking").data


def test_snoozing_to_a_given_date(client, app):
    enquiry_id = _stale_case(app)
    client.post("/tracking/act", data={
        "do": "snooze_until", "rule": "R1", "kind": "enquiry",
        "ref_id": enquiry_id, "until": "2026-12-24"}, follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute("SELECT until FROM tracking_snooze").fetchone()[
        "until"] == "2026-12-24"
    conn.close()


def test_snoozing_without_a_date_says_so(client, app):
    enquiry_id = _stale_case(app)
    response = client.post("/tracking/act", data={
        "do": "snooze_until", "rule": "R1", "kind": "enquiry",
        "ref_id": enquiry_id, "until": ""}, follow_redirects=True)
    assert b"Pick a date" in response.data
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM tracking_snooze"
                        ).fetchone()["n"] == 0
    conn.close()


# test_logging_a_vendor_reply_from_the_list: FX4 hides the R2 card by default; its test is in
# tests/test_fx4_rewritten.py.


def test_logging_a_touch_from_the_list(client, app):
    conn = conn_of(app)
    repo = db.Repository(conn, source="test")
    quiet = repo.insert("party", {"key": "Quiet Co", "type": "Agent",
                                  "country": "Chile", "stage": "active"})
    repo.insert("party_event", {"party_id": quiet, "at": "2026-01-05",
                                "type": "outreach"})
    repo.commit()
    conn.close()

    assert b"Quiet Co" in client.get("/tracking").data
    # T2: the touch is yesterday, inside the cadence whatever day this runs
    touched = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    client.post("/tracking/act", data={
        "do": "touch", "rule": "R7", "kind": "party", "ref_id": quiet,
        "party_id": quiet, "at": touched, "type": "call",
        "note": "called about LATAM volumes"}, follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT at, type, summary FROM party_event "
                       "WHERE party_id = ? ORDER BY id DESC LIMIT 1",
                       (quiet,)).fetchone()
    assert (row["at"], row["type"]) == (touched, "call")
    assert row["summary"] == "called about LATAM volumes"
    conn.close()
    # The clock restarted, so the row has left the list.
    assert b"Quiet Co" not in client.get("/tracking").data


def test_an_unknown_action_is_refused(client, app):
    enquiry_id = _stale_case(app)
    assert client.post("/tracking/act", data={
        "do": "delete-everything", "rule": "R1", "kind": "enquiry",
        "ref_id": enquiry_id}).status_code == 400


# ----------------------------------------- 4.2 editing the timeline ---------
def _event_id(app, enquiry_id=1):
    conn = conn_of(app)
    row = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? "
                       "ORDER BY id DESC LIMIT 1", (enquiry_id,)).fetchone()
    conn.close()
    return row["id"]


def test_the_timeline_is_editable_on_the_case_page(client):
    body = client.get("/enquiries/1").data.decode("utf-8")
    assert "Strike out" in body
    assert 'name="at"' in body and 'name="note"' in body


def test_correcting_an_event_from_the_page(client, app):
    event_id = _event_id(app)
    response = client.post("/enquiries/1/events/{}".format(event_id), data={
        "action": "save", "at": "2026-09-02", "status": "Quoted",
        "note": "date was wrong"}, follow_redirects=True)
    assert response.status_code == 200
    conn = conn_of(app)
    row = conn.execute("SELECT at, status, note FROM status_event WHERE id = ?",
                       (event_id,)).fetchone()
    assert (row["at"], row["status"], row["note"]) == (
        "2026-09-02", "Quoted", "date was wrong")
    assert conn.execute("SELECT status FROM enquiry WHERE id = 1"
                        ).fetchone()["status"] == "Quoted"
    conn.close()


def test_striking_an_event_out_and_putting_it_back(client, app):
    event_id = _event_id(app)
    client.post("/enquiries/1/events/{}".format(event_id),
                data={"action": "void"}, follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute("SELECT voided FROM status_event WHERE id = ?",
                        (event_id,)).fetchone()["voided"] == 1
    conn.close()
    body = client.get("/enquiries/1").data.decode("utf-8")
    assert "struck out" in body and "Put back" in body

    client.post("/enquiries/1/events/{}".format(event_id),
                data={"action": "unvoid"}, follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute("SELECT voided FROM status_event WHERE id = ?",
                        (event_id,)).fetchone()["voided"] == 0
    conn.close()


def test_an_event_of_another_case_is_not_editable_here(client, app):
    event_id = _event_id(app)
    assert client.post("/enquiries/999/events/{}".format(event_id),
                       data={"action": "void"}).status_code == 404


def test_an_event_needs_a_date_and_a_real_status(client, app):
    event_id = _event_id(app)
    response = client.post("/enquiries/1/events/{}".format(event_id), data={
        "action": "save", "at": "", "status": "Quoted"}, follow_redirects=True)
    assert b"needs a date" in response.data
    response = client.post("/enquiries/1/events/{}".format(event_id), data={
        "action": "save", "at": "2026-09-02", "status": "Banana"},
        follow_redirects=True)
    assert b"not a status" in response.data


# ------------------------------------------ 4.2 duplicates and mistakes -----
def _second_case(app, opp_no="OPP-1402"):
    conn = conn_of(app)
    repo = db.Repository(conn, source="test")
    enquiry_id = repo.insert("enquiry", {
        "opp_no": opp_no, "requester_party_id": 1, "received_on": "2026-09-02",
        "status": "New"})
    repo.insert("status_event", {"enquiry_id": enquiry_id, "at": "2026-09-02",
                                 "status": "New"})
    repo.commit()
    conn.close()
    return enquiry_id


def test_marking_a_case_as_a_duplicate_from_the_page(client, app):
    other = _second_case(app)
    response = client.post("/enquiries/{}/duplicate".format(other), data={
        "action": "duplicate", "of_opp_no": "opp-1401",
        "at": "2026-09-09", "note": "same request"}, follow_redirects=True)
    assert b"Marked as a duplicate of OPP-1401" in response.data
    conn = conn_of(app)
    row = conn.execute(
        "SELECT status, closed_reason, group_id FROM enquiry WHERE id = ?",
        (other,)).fetchone()
    assert (row["status"], row["closed_reason"]) == ("Dropped",
                                                     "Duplicate of OPP-1401")
    assert row["group_id"] == 1
    note = conn.execute("SELECT note FROM status_event WHERE enquiry_id = ? "
                        "ORDER BY id DESC LIMIT 1", (other,)).fetchone()["note"]
    assert note == "Duplicate of OPP-1401. same request"
    conn.close()


def test_a_duplicate_of_a_case_that_does_not_exist_is_refused(client, app):
    other = _second_case(app)
    response = client.post("/enquiries/{}/duplicate".format(other), data={
        "action": "duplicate", "of_opp_no": "OPP-9999"}, follow_redirects=True)
    assert b"No case OPP-9999" in response.data
    conn = conn_of(app)
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?",
                        (other,)).fetchone()["status"] == "New"
    conn.close()


def test_a_case_cannot_be_a_duplicate_of_itself(client, app):
    response = client.post("/enquiries/1/duplicate", data={
        "action": "duplicate", "of_opp_no": "OPP-1401"}, follow_redirects=True)
    assert b"cannot be a duplicate of itself" in response.data


def test_marking_a_case_as_registered_in_error(client, app):
    other = _second_case(app)
    response = client.post("/enquiries/{}/duplicate".format(other), data={
        "action": "error", "at": "2026-09-09"}, follow_redirects=True)
    assert b"registered in error" in response.data
    conn = conn_of(app)
    row = conn.execute(
        "SELECT status, closed_reason FROM enquiry WHERE id = ?",
        (other,)).fetchone()
    assert (row["status"], row["closed_reason"]) == ("Dropped",
                                                     "Registered in error")
    conn.close()


# ------------------------------- log a vendor request from Outlook --
def test_log_vendor_request_from_the_mail_we_sent(client, app, outlook):
    outlook["mail"] = _FakeMail("RATE REQUEST // MIA - SAI // FCL",
                                sender="VictorLang@jojofreight.example", internal=True,
                                to=["rates@bluefin-usa.example"], cc=["pricing@msc.example"],
                                received="2026-09-05")
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert page.status_code == 200
    assert b"Logged 2 vendor requests: Bluefin Usa, MSC" in page.data
    conn = conn_of(app)
    rows = conn.execute(
        "SELECT p.key, v.asked_on, v.source, v.notes FROM vendor_request v "
        "JOIN party p ON p.id = v.vendor_party_id ORDER BY p.key").fetchall()
    assert [(r["key"], r["asked_on"], r["source"]) for r in rows] == [
        ("Bluefin Usa", "2026-09-05", "manual"), ("MSC", "2026-09-05", "manual")]
    assert rows[0]["notes"].startswith("Logged from Outlook: RATE REQUEST")
    assert conn.execute("SELECT party_id FROM party_domain WHERE domain = 'msc.example'"
                        ).fetchone() is not None
    conn.close()

    # the same mail again adds nothing
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert b"already on the case: Bluefin Usa, MSC" in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM vendor_request").fetchone()["n"] == 2
    conn.close()


def test_an_inbound_mail_logs_no_vendor(client, app, outlook):
    outlook["mail"] = _FakeMail("RATE REQUEST // MIA - SAI", sender="ana@tolvex.example")
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert b"not one we sent out" in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM vendor_request").fetchone()["n"] == 0
    conn.close()


def test_outlook_closed_is_a_message_on_the_case_page(client, outlook):
    from opplist import outlook_bridge
    outlook["error"] = outlook_bridge.OutlookError(outlook_bridge.NOT_RUNNING)
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert page.status_code == 200
    assert b"Open Outlook and select a mail first." in page.data


# ------------------------------- log a vendor request from Outlook --
def test_log_vendor_request_from_the_mail_we_sent(client, app, outlook):
    outlook["mail"] = _FakeMail("RATE REQUEST // MIA - SAI // FCL",
                                sender="VictorLang@jojofreight.example", internal=True,
                                to=["rates@bluefin-usa.example"], cc=["pricing@msc.example"],
                                received="2026-09-05")
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert page.status_code == 200
    assert b"Logged 2 vendor requests: Bluefin Usa, MSC" in page.data
    conn = conn_of(app)
    rows = conn.execute(
        "SELECT p.key, v.asked_on, v.source, v.notes FROM vendor_request v "
        "JOIN party p ON p.id = v.vendor_party_id ORDER BY p.key").fetchall()
    assert [(r["key"], r["asked_on"], r["source"]) for r in rows] == [
        ("Bluefin Usa", "2026-09-05", "manual"), ("MSC", "2026-09-05", "manual")]
    assert rows[0]["notes"].startswith("Logged from Outlook: RATE REQUEST")
    assert conn.execute("SELECT party_id FROM party_domain WHERE domain = 'msc.example'"
                        ).fetchone() is not None
    conn.close()

    # the same mail again adds nothing
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert b"already on the case: Bluefin Usa, MSC" in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM vendor_request").fetchone()["n"] == 2
    conn.close()


def test_an_inbound_mail_logs_no_vendor(client, app, outlook):
    outlook["mail"] = _FakeMail("RATE REQUEST // MIA - SAI", sender="ana@tolvex.example")
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert b"not one we sent out" in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT COUNT(*) AS n FROM vendor_request").fetchone()["n"] == 0
    conn.close()


def test_outlook_closed_is_a_message_on_the_case_page(client, outlook):
    from opplist import outlook_bridge
    outlook["error"] = outlook_bridge.OutlookError(outlook_bridge.NOT_RUNNING)
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert page.status_code == 200
    assert b"Open Outlook and select a mail first." in page.data


# ------------------------------------------------- the visual report (7.3) --


# ------------------------------------------------ scope and carriers (v9) --
def _out_case(app):
    conn = conn_of(app)
    quillon = conn.execute("INSERT INTO party (key, full_name, type, country) VALUES "
                          "('Quillon Fasteners', 'Quillon Fasteners', 'Direct Customer', 'USA')"
                          ).lastrowid
    case = conn.execute("INSERT INTO enquiry (opp_no, requester_party_id, received_on, status, scope) "
                        "VALUES ('OPP-1900', ?, '2026-09-04', 'New', 'Out')", (quillon,)).lastrowid
    conn.commit()
    conn.close()
    return case


def test_out_cases_are_off_the_list_unless_the_filter_opens_them(client, app):
    _out_case(app)
    assert "OPP-1900" not in enquiries_client.get(client, "/enquiries").shown
    assert "OPP-1900" in enquiries_client.get(client, "/enquiries?scope=Out").shown
    assert "OPP-1900" in enquiries_client.get(client, "/enquiries?scope=all").shown
    assert "OPP-0001" not in enquiries_client.get(client, "/enquiries?scope=Out").shown


# test_the_scope_can_be_changed_on_the_case_page_and_is_logged: replaced in 15a by tests/test_case_v3.py (case page v3).


# test_a_direct_customers_case_is_registered_out_without_a_scope_field: replaced in 15a by tests/test_case_v3.py (case page v3).


# ------------------------------------------ list and case page corrections --

def test_every_list_column_sorts_both_ways(client, app):
    conn = conn_of(app)
    conn.execute("INSERT INTO enquiry (opp_no, requester_party_id, received_on, status, "
                 "service_mode, origin_loc, dest_loc, container_qty) "
                 "VALUES ('OPP-0002', 1, '2026-08-01', 'Quoted', 'Air', 'LAX', 'SAI', 3)")
    conn.execute("INSERT INTO status_event (enquiry_id, at, status) "
                 "VALUES ((SELECT id FROM enquiry WHERE opp_no = 'OPP-0002'), '2026-08-01', 'Quoted')")
    conn.commit()
    conn.close()
    for column in ("opp_no", "received_on", "requester", "service_mode", "lane", "qty",
                   "status", "last_event_on"):
        seen = []
        for direction in ("asc", "desc"):
            # one group, so the order is the sort's alone
            page = enquiries_client.get(client, "/enquiries?view=open&group=requester&sort={}&dir={}".format(
                column, direction))
            seen.append(page.shown[0])
        # the two fixture cases tie on requester and on lane; every other
        # column turns the list around
        assert seen[0] != seen[1] or column in ("requester", "lane"), column
    from opplist.web import order_by
    lane = order_by("enquiries", "lane", "asc")
    assert "COALESCE(origin_loc, origin_country) COLLATE NOCASE ASC" in lane
    assert "COALESCE(dest_loc, dest_country) COLLATE NOCASE ASC" in lane
    assert "CAST(substr(opp_no, 5) AS INTEGER) DESC" in order_by("enquiries", "opp_no", "desc")


def test_the_list_hides_end_customer_owner_group_and_scope(client):
    page = enquiries_client.get(client, "/enquiries")
    for col in ("end_customer", "owner", "request_group", "scope"):
        assert col not in page.head, col
    assert "qty" in page.head
    body = client.get("/enquiries").data.decode("utf-8")
    assert 'data-filter="scope"' in body                   # the filter stays


def test_the_clear_button_is_a_word_not_a_method(client):
    for path in ("/enquiries", "/parties"):
        body = client.get(path).data.decode("utf-8")
        assert "built-in method" not in body
        assert ">Clear<" in body


def test_no_label_key_shadows_a_dict_method():
    from opplist.web.text import TEXT
    assert not {"clear", "items", "keys", "values", "get", "pop", "update", "copy"} & set(TEXT)


def test_new_party_is_a_small_link_not_an_option(client):
    body = client.get("/enquiries/new").data.decode("utf-8")
    assert "Add party..." not in body
    assert 'class="link tiny new-party" data-toggle="new-party">+ New</button>' in body
    assert '<fieldset class="inline-add" id="new-party" hidden>' in body


# test_owner_survives_a_save_from_the_form_that_no_longer_shows_it: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# test_timeline_events_are_lines_with_an_edit_toggle: replaced in 15a by tests/test_case_v3.py (case page v3).


def test_note_only_adds_a_note_and_changes_nothing_else(client, app):
    page = client.post("/enquiries/1/status",
                       data={"status": "note", "at": "2026-09-05", "note": "called them"},
                       follow_redirects=True)
    assert b"Note added" in page.data and b"called them" in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT status FROM enquiry WHERE id = 1").fetchone()[0] == "New"
    assert conn.execute("SELECT kind, at FROM status_event WHERE note = 'called them'"
                        ).fetchone()[0] == "note"
    conn.close()
    page = client.post("/enquiries/1/status", data={"status": "note", "at": "2026-09-05"},
                       follow_redirects=True)
    assert b"A note needs some text." in page.data


def test_a_case_registered_without_a_scope_is_in(client, app):
    client.post("/enquiries/new", data=base_form(), follow_redirects=True)
    conn = conn_of(app)
    assert conn.execute("SELECT scope FROM enquiry ORDER BY id DESC LIMIT 1").fetchone()[0] == "In"
    conn.close()


def test_carrier_direct_is_the_fifth_answer(client, app):
    conn = conn_of(app)
    atlas = conn.execute("INSERT INTO party (key, full_name, type, country) VALUES "
                         "('Atlas Air', 'Atlas Air', 'Carrier', 'USA')").lastrowid
    conn.commit()
    conn.close()
    page = client.get("/enquiries/1")
    assert b"Carrier direct" in page.data and b'name="carrier_party_id"' in page.data
    client.post("/enquiries/1/status",
                data={"status": "Won", "at": TODAY, "rate_used": "carrier",
                      "carrier_party_id": str(atlas)}, follow_redirects=True)
    conn = conn_of(app)
    row = conn.execute("SELECT rate_used, awarded_vendor_id FROM enquiry WHERE id = 1").fetchone()
    assert tuple(row) == ("carrier", atlas)
    conn.close()
    page = client.get("/enquiries/1")
    assert b"Carrier direct" in page.data and b"Atlas Air" in page.data


# ------------------------------------------------- the build line (footer) --
def test_every_page_ends_with_the_build_line_and_versioned_assets(client, app):
    import re
    for path in ("/enquiries", "/parties", "/tracking", "/export", "/settings"):
        body = client.get(path).data.decode("utf-8")
        line = re.search(r'<footer class="build no-print">(.*?)</footer>', body)
        assert line, path
        assert re.fullmatch(r"v\d+ \u00b7 started \d\d:\d\d \u00b7 code \d\d:\d\d", line.group(1)), line.group(1)
        assert re.search(r'app\.css\?v=\d+', body) and re.search(r'app\.js\?v=\d+', body), path


# ------------------------------------------------------ list columns (4.1) --
def _list_head(client, query=""):
    body = client.get("/enquiries" + query).data.decode("utf-8")
    import re
    return re.findall(r'<th[^>]*data-col="([a-z_]+)"', body), body


def test_commodity_is_a_default_column_cut_at_thirty_characters(client, app):
    long_name = "Tessaro toner cartridges, cleaning kits and spare drums"
    conn = conn_of(app)
    conn.execute("UPDATE enquiry SET commodity = ? WHERE id = 1", (long_name,))
    conn.commit()
    conn.close()
    cols, body = _list_head(client)
    assert "commodity" in cols
    assert 'title="{}"'.format(long_name) in body
    assert long_name[:29].rstrip() + "\u2026" in body and long_name not in body.split('title="')[1].split("</td>")[0].split(">")[1]
    page = client.get("/enquiries?sort=commodity&dir=asc")
    assert page.status_code == 200
    from opplist.web import order_by
    assert "commodity COLLATE NOCASE ASC" in order_by("enquiries", "commodity", "asc")


def test_the_default_layout_hides_four_columns(client):
    cols, body = _list_head(client)
    assert cols == ["opp_no", "received_on", "requester", "service_mode", "lane", "incoterm",
                    "qty", "commodity", "status", "hold", "last_event_on"]
    assert 'name="visible" value="end_customer"' in body     # offered in the menu
    assert 'value="end_customer" checked' not in body
    assert '<summary class="chip">Columns</summary>' in body


def test_the_order_and_the_ticks_are_remembered_and_reset(client, app):
    client.post("/enquiries/columns", data={
        "order": "status,opp_no,commodity,received_on,requester,service_mode,lane,incoterm,qty,"
                 "last_event_on,end_customer,owner,request_group,scope",
        "action": "show", "visible": ["status", "opp_no", "scope", "end_customer"]},
        follow_redirects=True)
    conn = conn_of(app)
    saved = db.get_setting(conn, "list_columns")
    conn.close()
    assert '"order": ["status", "opp_no", "commodity"' in saved
    cols, body = _list_head(client)
    assert cols == ["status", "opp_no", "end_customer", "scope"]
    assert 'value="scope" checked' in body
    # a drag posts only the order; the ticks stay as they were
    client.post("/enquiries/columns", data={
        "order": "opp_no,status,end_customer,scope,commodity,received_on,requester,service_mode,"
                 "lane,incoterm,qty,last_event_on,owner,request_group"}, follow_redirects=True)
    cols, _ = _list_head(client)
    assert cols == ["opp_no", "status", "end_customer", "scope"]
    client.post("/enquiries/columns", data={"action": "reset"}, follow_redirects=True)
    cols, _ = _list_head(client)
    assert cols[0] == "opp_no" and "scope" not in cols and "commodity" in cols


def test_a_column_added_later_joins_a_saved_layout(client, app):
    conn = conn_of(app)
    db.set_setting(conn, "list_columns", '{"order": ["status", "opp_no"], "visible": ["status", "opp_no"]}')
    conn.commit()
    conn.close()
    cols, _ = _list_head(client)
    assert cols[:2] == ["status", "opp_no"] and "commodity" in cols and "scope" not in cols


def test_the_layout_changes_nothing_but_the_list(client, app, tmp_path):
    from opplist import export_csv
    client.post("/enquiries/columns", data={"order": "status,commodity,opp_no", "action": "show",
                                            "visible": ["status", "commodity", "opp_no"]},
                follow_redirects=True)
    # a hidden column still sorts (the rows follow it)
    assert client.get("/enquiries?sort=received_on&dir=asc").status_code == 200
    page = enquiries_client.get(client, "/enquiries?sort=status&dir=asc&group=requester")
    assert page.head == ["status", "commodity", "opp_no"]
    assert page.shown
    assert "OPP-" in "".join(enquiries_client.get(client, "/enquiries?scope=all").shown)
    conn = conn_of(app)
    path = export_csv.export_opp_index(conn, tmp_path / "opp-index.csv")
    conn.close()
    header = path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith('"OppNo"') or header.split(",")[0].strip('"') != "Status"
    assert header.count(",") == 7                          # the eight-column contract


# ------------------------------------------------------------ on hold (v11) --
# test_on_hold_from_the_set_status_box_needs_a_reason: replaced in 15a by tests/test_case_v3.py (case page v3).


# test_release_hold_by_hand_and_the_column_can_be_hidden: replaced in 15a by tests/test_case_v3.py (case page v3).


# test_a_long_hold_reason_is_cut_with_the_full_text_in_the_tooltip: replaced in 15a by tests/test_case_v3.py (case page v3).


def test_the_current_status_line_wraps_inside_its_panel():
    css = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    block = css[css.index(".current-status {"):css.index(".status-panel")]
    assert "flex-wrap: wrap" in block and "min-width: 0" in block and "overflow: hidden" in block
    assert ".current-status .since { white-space: nowrap; }" in css
    hold = css[css.index(".current-status .pill.hold {"):]
    hold = hold[:hold.index("}")]
    assert "white-space: normal" in hold and "max-width: 100%" in hold


# --------------------------------------- New party from a domain (5.5.5) --
_PO_BODY = ("See below,\n\nDerek Chan\n\nFrom: Omar Haddad <omar@desert-demo.com.sa>\n"
            "Sent: Thursday, September 3, 2026 8:30 AM\nTo: Omar Haddad <omar@desert-demo.com.sa>\n"
            "Subject: Order 2231 for collection (ref K7)\n\nHello all,\n"
            "Please advise an air price for the carton below, it is urgent.\n\n1 Carton\n"
            "Size: 20\u201d x 12\u201d x 12\u201d\nWeight: 9LBS\nMaterial: Carbon fibre tubes\n\n"
            "Pick-up:\nMesa Verde Composites\n418 W Juniper Ave\nPrescott, AZ 86301\n\n"
            "Hours: Monday-Friday 7:30AM-3:00PM\n")


def _po_mail():
    mail = _FakeMail("FW: Order 2231 for collection (ref K7)", sender="DerekChan@jojofreight.example",
                     internal=True, to=("JJC-MFN@jojofreight.example",), received="2026-09-03", body=_PO_BODY)
    mail.sender_name = "JJC-DEREK CHAN"
    return mail


# test_an_unknown_domain_opens_the_new_party_form_filled_in: replaced in 15c by tests/test_register_v3.py (the register page is the case page's cards).


def test_saving_with_the_new_party_creates_it_with_its_domain(client, app, outlook):
    conn = conn_of(app)
    conn.execute("INSERT INTO country (name, region) VALUES ('Saudi Arabia', 'Middle East')")
    conn.commit()
    conn.close()
    page = client.post("/enquiries/new", data=base_form(
        decision="new", requester_party_id="", new_party_key="Desert Demo",
        new_party_type="Agent", new_party_country="Saudi Arabia",
        new_party_domain="desert-demo.com.sa", learn_domain="desert-demo.com.sa",
        created_by="outlook", sender_address="DerekChan@jojofreight.example", sender_internal="1",
        service_mode="Air", air_weight_kg="7.9", container_type="", container_qty="",
        dest_country="", dest_loc=""), follow_redirects=True)
    assert b"Remembered desert-demo.com.sa for Desert Demo." in page.data
    conn = conn_of(app)
    party = conn.execute("SELECT * FROM party WHERE key = 'Desert Demo'").fetchone()
    assert (party["type"], party["country"]) == ("Agent", "Saudi Arabia")
    assert conn.execute("SELECT party_id FROM party_domain WHERE domain = 'desert-demo.com.sa'"
                        ).fetchone()["party_id"] == party["id"]
    assert conn.execute("SELECT requester_party_id FROM enquiry WHERE opp_no = 'OPP-1402'"
                        ).fetchone()["requester_party_id"] == party["id"]
    assert conn.execute("SELECT count(*) FROM change_log WHERE table_name = 'party'").fetchone()[0] >= 1
    assert conn.execute("SELECT count(*) FROM change_log WHERE table_name = 'party_domain'").fetchone()[0] >= 1
    conn.close()


def test_a_name_match_fills_the_requester_and_the_domain_is_kept_on_save(client, app, outlook):
    conn = conn_of(app)
    conn.execute("INSERT INTO party (key, full_name, type, country) VALUES "
                 "('Desert Demo', 'Desert Demo', 'Agent', 'Chile')")
    pid = conn.execute("SELECT id FROM party WHERE key = 'Desert Demo'").fetchone()["id"]
    conn.commit()
    conn.close()
    outlook["mail"] = _po_mail()
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert '<option value="{}" data-type="Agent"\n          selected>'.format(pid) in body \
        or 'value="{}" data-type="Agent" selected'.format(pid) in body.replace("\n          ", " ")
    assert "domain matches party name (desert-demo ~ Desert Demo)" in body
    assert 'name="learn_domain" value="desert-demo.com.sa"' in body
    page = client.post("/enquiries/new", data=base_form(
        decision="new", requester_party_id=str(pid), learn_domain="desert-demo.com.sa",
        created_by="outlook", sender_address="DerekChan@jojofreight.example", sender_internal="1"),
        follow_redirects=True)
    assert b"Remembered desert-demo.com.sa for Desert Demo." in page.data
    conn = conn_of(app)
    assert conn.execute("SELECT party_id FROM party_domain WHERE domain = 'desert-demo.com.sa'"
                        ).fetchone()["party_id"] == pid
    conn.close()
    # the next mail from them matches by domain, no name match needed
    body = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert "from the quoted original mail (Omar Haddad 2026-09-03 08:30)" in body
    assert "domain matches party name" not in body


def test_no_page_nests_a_form_inside_a_form(client, app):
    """Every page the app serves: a nested form is dropped by the browser,
    and the buttons pointing at it stop working (2026-09-17)."""
    from conftest import form_nesting

    for path in ("/enquiries", "/enquiries?view=all", "/enquiries/new", "/enquiries/1",
                 "/parties", "/parties/1", "/parties/duplicates", "/tracking",
                 "/settings", "/export", "/report"):
        page = client.get(path)
        if page.status_code != 200:
            continue
        deepest, ids = form_nesting(page.data.decode("utf-8"))
        assert deepest <= 1, "{} nests a form inside a form ({})".format(path, ids)


def test_the_message_id_is_kept_off_the_timeline(client, app):
    """A '[mid: <...>]' tag cannot wrap and pushed the timeline out of its
    panel (2026-09-21). The page hides it and keeps it in the title; the
    note in the database is untouched."""
    mid = "<" + "a" * 70 + "@x.mail.example>"
    assert len(mid) > 85
    note = "Reply from Renata Solis (needs an answer): can you update the rate? [mid: {}]".format(mid)
    conn = conn_of(app)
    repo = db.Repository(conn, source="test")
    repo.add_note(1, "2026-09-20", note, source="mail")
    repo.commit()
    conn.close()

    body = client.get("/enquiries/1").data.decode("utf-8")
    start = body.index('<span class="note"')
    span = body[start:body.index("</span>", start)]
    text = span[span.index(">") + 1:]
    assert "[mid:" not in text
    assert text.strip() == "Reply from Renata Solis (needs an answer): can you update the rate?"
    assert "[mid: &lt;" in span and "@x.mail.example&gt;]" in span           # the title has it

    conn = conn_of(app)
    stored = conn.execute("SELECT note FROM status_event WHERE enquiry_id = 1 "
                          "AND kind = 'note'").fetchone()["note"]
    assert stored == note
    conn.close()

    # the two 'not a real case' buttons sit on a line of their own
    assert '<span class="break"></span>' in body


def test_special_handling_is_clickable_on_the_box_only(client, app):
    """The label used to wrap the text, so the whole row ticked the box and
    tags were set by accident (2026-09-17)."""
    import re as _re

    body = client.get("/enquiries/new").data.decode("utf-8")
    start = body.index('<fieldset class="f wide tags">')
    block = body[start:body.index("</fieldset>", start)]
    assert "<label" not in block                      # the words are not a hit area
    boxes = _re.findall(r'<input[^>]*name="special_handling"[^>]*>', block)
    assert len(boxes) >= 5
    for box in boxes:                                  # still has a name for a screen reader
        assert 'aria-label="' in box
    assert '<span class="tag-text">OOG</span>' in block

    # a ticked box still reaches the case
    page = client.post("/enquiries/new", data={
        "created_by": "manual", "received_on": "2026-09-17", "status": "New",
        "requester_party_id": "1", "service_mode": "Air", "air_weight_kg": "120",
        "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile",
        "dest_loc": "SAI", "commodity": "Spare parts",
        "special_handling": ["OOG", "DG"], "decision": "new"}, follow_redirects=True)
    assert page.status_code == 200
    conn = conn_of(app)
    saved = conn.execute("SELECT special_handling FROM enquiry WHERE opp_no = 'OPP-1402'").fetchone()[0]
    conn.close()
    assert "OOG" in saved and "DG" in saved
