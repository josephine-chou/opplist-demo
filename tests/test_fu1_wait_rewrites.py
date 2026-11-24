"""Batch L2: the follow-up ladder's older checks, rewritten.

These replace eleven tests that pinned FU2 after an unanswered FU1 and
'No response from customer' for every Decide row; they were written on
cases of the first data set, so they were rewritten here with invented data
rather than edited (test_followup, test_case_page_v2, test_case_page_v2b,
test_vendor_quoted_and_due, test_detect_followups). Each check is kept,
on the path the new rule gives it: FU2 where the requester answered FU1,
Decide where they did not.

Every company, person, place, number and date here is invented; cases
from OPP-6921.
"""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, tracking
from opplist import web as web_module
from opplist.web import create_app


def r3(conn, today) -> dict:
    section = {s.rule: s for s in tracking.run(conn, today=today)}["R3"]
    return {i.label: i for i in section.items}


@pytest.fixture
def agent(repo):
    seed_reference(repo)
    party_id = add_party(repo, "Tarnmoor Logistics", country="Chile")
    repo.commit()
    return party_id


def quoted(repo, party_id, opp_no, quoted_on, **extra):
    case = add_enquiry(repo, opp_no, party_id, received="2026-11-02", **extra)
    repo.set_status(case, "Quoted", quoted_on)
    return case


# ------------------------------------------------------------ fu_step --
def test_fu_step_alone():
    s = tracking.FU_DEFAULTS
    assert tracking.fu_step(0, 0, s) == "" and tracking.fu_step(0, 1, s) == "FU1"
    assert tracking.fu_step(1, 4, s, answered=True) == "" and tracking.fu_step(1, 5, s, answered=True) == "FU2"
    assert tracking.fu_step(1, 5, s) == "" and tracking.fu_step(1, 10, s) == "Decide"     # no answer
    assert tracking.fu_step(2, 4, s) == "" and tracking.fu_step(2, 5, s) == "Decide"
    assert tracking.fu_step(3, 5, s) == "Decide"                # past fu_max: only Decide
    assert tracking.fu_step(0, None, s) == ""


def test_fu2_is_due_five_working_days_after_the_answer(repo, conn, agent):
    a = quoted(repo, agent, "OPP-6921", "2026-11-02")
    b = quoted(repo, agent, "OPP-6922", "2026-11-02")
    for case, day in ((a, "2026-11-04"), (b, "2026-11-05")):        # Wed: 5 working days to the 11th; Thu: 4
        repo.add_followup(case, day)
        repo.add_note(case, day, "Called Lio: the consignee is still deciding")
    repo.commit()
    found = r3(conn, "2026-11-11")
    assert list(found) == ["OPP-6921"]
    assert (found["OPP-6921"].extra["step"], found["OPP-6921"].days, found["OPP-6921"].since) == \
        ("FU2", 5, "2026-11-04")


def test_the_settings_drive_the_ladder(repo, conn, agent):
    a = quoted(repo, agent, "OPP-6923", "2026-11-02")
    repo.add_followup(a, "2026-11-10")
    repo.add_note(a, "2026-11-10", "WhatsApp from Lio: will revert")
    db.set_setting(conn, "fu2_days", 1)
    repo.commit()
    assert r3(conn, "2026-11-11")["OPP-6923"].extra["step"] == "FU2"
    db.set_setting(conn, "fu_max", 1)
    repo.commit()
    assert "OPP-6923" not in r3(conn, "2026-11-11")      # one FU is the maximum now, fu_decide_days (5) not up
    db.set_setting(conn, "fu_decide_days", 1)
    repo.commit()
    assert r3(conn, "2026-11-11")["OPP-6923"].extra["step"] == "Decide"
    # FX4: the Decide card is hidden by default, and the badge leaves its rows out
    assert tracking.focus_count(conn, today="2026-11-11") == 0
    db.set_setting(conn, tracking.HIDDEN_SETTING, "r9,r2,r6")
    repo.commit()
    assert tracking.focus_count(conn, today="2026-11-11") == 1


def test_log_as_sent_moves_the_tracking_row_to_the_next_step(tmp_path, monkeypatch):
    path = tmp_path / "l2-fu.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Tarnmoor Logistics", country="Chile")
    case = quoted(repo, party, "OPP-6924", "2026-11-05", origin_loc="ORD", origin_country="USA",
                  dest_loc="SCL", dest_country="Chile", service_mode="FCL", container_type="40HQ", container_qty=2)
    repo.commit()
    conn.close()
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-06")
    client = create_app(path).test_client()
    assert 'class="pill p-navy">FU1 due</span>' in client.get("/tracking").data.decode("utf-8")
    page = client.post("/enquiries/{}/followups".format(case),
                       data={"step": "1", "at": "2026-11-06", "channel": "mail"}, follow_redirects=True)
    assert page.status_code == 200 and b"FU1 logged (mail, 2026-11-06)." in page.data
    body = page.data.decode("utf-8")
    assert "FU1 &middot; mail &middot; 11/6" in body
    assert "<td class=\"mono\">FU1</td><td class=\"mono\">2026-11-06</td>" in body
    assert '<span class="k">FU2</span>' in body                        # FX4: the next one logged is FU2
    conn = db.open_db(path)
    row = conn.execute("SELECT fu_count, last_fu_seq, status FROM v_enquiry WHERE id = ?", (case,)).fetchone()
    assert tuple(row) == (1, 1, "Quoted")
    # no answer: not FU2 five working days on, Decide on the tenth
    assert "OPP-6924" not in r3(conn, "2026-11-13")
    found = r3(conn, "2026-11-20")
    assert found["OPP-6924"].extra["step"] == "Decide" and found["OPP-6924"].since == "2026-11-06"
    conn.close()
    body = client.get("/enquiries?view=all").data.decode("utf-8")
    assert "FU1 &middot; 11/6" in body


# ------------------------------------------------- the case page ----
@pytest.fixture
def pages(tmp_path, monkeypatch):
    """Quoted Mon Nov 9 (received Mon Nov 2), FU1 Wed Nov 11; 'answered'
    has a call noted the same day, 'silent' has nothing after FU1."""
    path = tmp_path / "l2-case.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tarn = add_party(repo, "Tarnmoor Logistics", country="Chile")
    ostrand = add_party(repo, "Ostrand Carriers", country="USA")
    ids = {}
    for key, opp in (("answered", "OPP-6925"), ("silent", "OPP-6926")):
        case = add_enquiry(repo, opp, tarn, received="2026-11-02", service_mode="FCL", container_type="40GP",
                           container_qty=1, incoterm="EXW", origin_country="USA", origin_loc="ORD",
                           dest_country="Chile", dest_loc="SCL", commodity="Ceramic floor tiles")
        repo.set_status(case, "Quoted", "2026-11-09")
        repo.add_followup(case, "2026-11-11")
        repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": ostrand, "asked_on": "2026-11-09",
                                       "replied_on": "2026-11-09", "machine_status": "Quoted",
                                       "their_ref": "QT-77120"})
        ids[key] = case
    repo.add_note(ids["answered"], "2026-11-11", "Called Lio: the consignee decides next week")
    # due-now cases for Wed Nov 18: an answered FU1 of Nov 11, an answered
    # FU1 of Mon Nov 16 (due in three working days), an unanswered FU1 of Wed Nov 4
    for key, opp, fu, note in (("due", "OPP-6927", "2026-11-11", True), ("waiting", "OPP-6928", "2026-11-16", True),
                               ("decide", "OPP-6929", "2026-11-04", False)):
        case = add_enquiry(repo, opp, tarn, received="2026-11-02", service_mode="Air", origin_country="USA",
                           dest_country="Chile")
        repo.set_status(case, "Quoted", "2026-11-03")
        repo.add_followup(case, fu)
        if note:
            repo.add_note(case, fu, "Lio on the phone: still with the client")
        ids[key] = case
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def page(client, case_id):
    response = client.get("/enquiries/{}".format(case_id))
    assert response.status_code == 200
    return response.data.decode("utf-8")


def tiles(body):
    facts = body[body.index('<div class="facts">'):body.index('<div class="layout">')]
    labels = re.findall(r'<div class="f-l">(.*?)</div>', facts)
    values = [(v.strip(), s.strip()) for v, s in
              re.findall(r'<div class="f-v[^"]*">(.*?)</div>\s*<div class="f-s">(.*?)</div>', facts, re.S)]
    return labels, values


def rail(body):
    start = body.index('<ol class="sections">')
    text = body[start:body.index("</ol>", start)]
    return {key: (cls, state) for cls, key, state in re.findall(
        r'<li class="sec ([^"]*)"><a href="#(\w+)">.*?<span class="s-name">[^<]*</span>'
        r'(?:<span class="state">([^<]*)</span>)?</a>', text)}


def test_the_quoted_facts_count_working_days_and_name_the_next_rung(pages, monkeypatch):
    client, path, ids = pages
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-13")
    labels, values = tiles(page(client, ids["answered"]))
    assert labels == ["Received", "Quoted", "Last follow-up", "Next", "Vendor quote"]
    assert values[0] == ("Nov 2", "Mon") and values[1] == ("Nov 9", "5 working days")
    assert values[2][0] == "FU1 Nov 11" and "working days ago" in values[2][1]
    assert values[3][0].startswith("FU2 ")
    assert values[4] == ("QT-77120", "Ostrand Carriers · Nov 9")
    body = page(client, ids["answered"])
    assert "Next step" in body and "Log FU2" in body                   # FX4: no draft, its log
    # no answer to FU1: the next rung is the decision, and no FU2 draft
    labels, values = tiles(page(client, ids["silent"]))
    assert values[3][0] == "Decide Nov 25"
    body = page(client, ids["silent"])
    nxt = body[body.index('<div class="next">'):body.index("</aside>")]
    assert "Decide Wed Nov 25" in nxt and "Log FU2" not in nxt and "Draft FU" not in nxt


def test_the_ladder_helper_puts_dates_on_the_rungs(pages):
    client, path, ids = pages
    conn = db.open_db(path)
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids["answered"],)).fetchone()
    rung = tracking.ladder(conn, row, today="2026-11-13")
    assert rung["quoted"] == "2026-11-09" and rung["next"] == "FU2"
    assert rung["next_due"] == "2026-11-18"                         # the answer on the 11th + 5 working days
    assert rung["days_left"] == 3
    assert [(s[0], s[2]) for s in rung["steps"]] == [
        ("Quoted", "done"), ("FU1", "done"), ("FU2 due", "now"), ("Decide", "todo")]
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids["silent"],)).fetchone()
    rung = tracking.ladder(conn, row, today="2026-11-13")
    assert (rung["next"], rung["next_due"], rung["days_left"]) == ("Decide", "2026-11-25", 8)
    assert [(s[0], s[2]) for s in rung["steps"]] == [("Quoted", "done"), ("FU1", "done"), ("Decide", "now")]
    conn.close()
    assert tracking.add_business_days("2026-11-13", 1) == "2026-11-16"  # Friday + 1 = Monday


def test_the_rail_states_the_follow_ups(pages, monkeypatch):
    client, path, ids = pages
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-13")
    for key in ("answered", "silent"):
        entries = rail(page(client, ids[key]))
        assert entries["followups"] == ("", "FU1 sent") and entries["status"] == ("", "Quoted")
        assert entries["vendors"] == ("", "1 asked")


def test_the_ladder_says_due_today_and_in_three_days(pages):
    client, path, ids = pages
    conn = db.open_db(path)
    for key, want in (("due", ("FU2", "2026-11-18", 0)), ("waiting", ("FU2", "2026-11-23", 3)),
                      ("decide", ("Decide", "2026-11-18", 0))):
        row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids[key],)).fetchone()
        rung = tracking.ladder(conn, row, today="2026-11-18")
        assert (rung["next"], rung["next_due"], rung["days_left"]) == want, key
    conn.close()


def test_due_now_is_marked_in_the_three_places(pages, monkeypatch):
    client, path, ids = pages
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-18")
    body = page(client, ids["due"])
    facts = body[body.index('<div class="facts">'):body.index('<div class="layout">')]
    assert facts.count('<div class="fact due">') == 1
    assert "FU2 Nov 18" in facts[facts.index('class="fact due"'):] and "due now" in facts
    assert '<div class="n-title due">FU2 Wed Nov 18</div>' in body
    assert '<span class="l-step now due">FU2 due Nov 18</span>' in body
    body = page(client, ids["decide"])
    assert '<div class="n-title due">Decide Wed Nov 18</div>' in body
    assert '<span class="l-step now due">Decide Nov 18</span>' in body
    body = page(client, ids["waiting"])
    assert "fact due" not in body and "n-title due" not in body and "now due" not in body
    assert '<span class="l-step now">FU2 due Nov 23</span>' in body and "in 3 working days" in body


# --------------------------------------------------- the batch button --
@pytest.fixture
def decide(tmp_path):
    """Two old quotes on Decide and a fresh FU1, all for a requester that
    is not an agent or an office (a customer's reason)."""
    path = tmp_path / "l2-decide.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    party = add_party(repo, "Fenwick Grain Co", ptype="Unknown", country="USA")
    ids = {}
    for n, opp, day in ((1, "OPP-6931", "2026-08-03"), (2, "OPP-6932", "2026-08-04"), (3, "OPP-6933", "2026-11-12")):
        case = add_enquiry(repo, opp, party, received="2026-07-30")
        repo.set_status(case, "Quoted", day)
        ids[n] = case
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "No Response from Customer", "Other"])
    repo.commit()
    conn.close()
    return create_app(path).test_client(), path, ids


def test_decide_rows_have_a_checkbox_and_the_batch_form(decide, monkeypatch):
    client, path, ids = decide
    monkeypatch.setattr(web_module, "_today", lambda: "2026-11-13")
    body = client.get("/tracking").data.decode("utf-8")
    assert 'class="decide-pick"' not in body and "Decide 2" in body       # FX4: hidden by default
    conn = db.open_db(path)
    db.set_setting(conn, tracking.HIDDEN_SETTING, "r9,r2,r6")
    conn.commit()
    conn.close()
    body = client.get("/tracking").data.decode("utf-8")
    assert body.count('class="decide-pick"') == 2                 # the two old ones, not the fresh FU1
    assert 'form="decide-R3"' in body and 'id="decide-R3"' in body
    assert "Mark selected Lost: No response<" in body
    assert "select the 2 shown" in body


def test_the_batch_keeps_the_existing_reason_spelling(decide):
    client, path, ids = decide
    page = client.post("/tracking/decide-lost", data={"ids": [str(ids[1]), str(ids[3])], "at": "2026-11-13"},
                       follow_redirects=True)
    assert page.status_code == 200
    assert b"Marked Lost (No Response from Customer): OPP-6931, OPP-6933" in page.data
    conn = db.open_db(path)
    rows = {r["opp_no"]: (r["status"], r["closed_reason"]) for r in
            conn.execute("SELECT opp_no, status, closed_reason FROM enquiry")}
    assert rows == {"OPP-6931": ("Lost", "No Response from Customer"), "OPP-6932": ("Quoted", None),
                    "OPP-6933": ("Lost", "No Response from Customer")}
    event = conn.execute("SELECT at, status, source FROM status_event WHERE enquiry_id = ? "
                         "ORDER BY id DESC LIMIT 1", (ids[1],)).fetchone()
    assert tuple(event) == ("2026-11-13", "Lost", "manual")
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'enquiry' AND field = 'status' "
                        "AND new_value = 'Lost'").fetchone()[0] == 2
    assert db.get_setting_list(conn, "closed_reasons_lost") == ["Price too high", "No Response from Customer", "Other"]
    conn.close()
    page = client.post("/tracking/decide-lost", data={"at": "2026-11-13"}, follow_redirects=True)
    assert b"Tick the cases to mark Lost first." in page.data


def test_the_reason_is_added_when_the_settings_lack_it(decide):
    client, path, ids = decide
    conn = db.open_db(path)
    db.set_setting(conn, "closed_reasons_lost", ["Price too high", "Other"])
    conn.commit()
    conn.close()
    page = client.post("/tracking/decide-lost", data={"ids": [str(ids[2])], "at": "2026-11-13"},
                       follow_redirects=True)
    assert b"Marked Lost (No response from customer): OPP-6932" in page.data
    conn = db.open_db(path)
    assert db.get_setting_list(conn, "closed_reasons_lost") == ["Price too high", "Other", "No response from customer"]
    conn.close()
