"""No edit lost (2026-09-28): the case page saves what was posted and names
the gaps; a value that cannot be saved writes nothing and says so in a Not
saved banner; the save-first JSON answer the page script uses; one line per
subject; the register form keeps its rules."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db
from opplist.web import create_app

TODAY = "2026-09-28"
JS = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.js").read_text(encoding="utf-8")
SAVE_FIRST = {"X-Save-First": "1", "Accept": "application/json"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "b13.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    ids = {"tolvex": tolvex}
    # OPP-1298's shape: FCL, DAP, no delivery place
    ids["dap"] = add_enquiry(repo, "OPP-1298", tolvex, received="2026-09-10", service_mode="FCL",
                             container_type="40HQ", container_qty=1, incoterm="DAP", commodity="Peas",
                             origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile",
                             source_subject="RATE REQUEST // MIA - SAI")
    repo.set_status(ids["dap"], "Quoted", "2026-09-12")
    ids["bare"] = add_enquiry(repo, "OPP-1299", tolvex, received="2026-09-10", service_mode="Air",
                              origin_country="USA", dest_country="Chile", air_weight_kg=100)
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def form_of(**changes):
    values = {"received_on": "2026-09-10", "requester_party_id": "1", "service_mode": "FCL",
              "container_type": "40HQ", "container_qty": "1", "incoterm": "DAP", "commodity": "Peas",
              "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile", "dest_loc": "SAI",
              "dest_alt": "", "source_subject": "RATE REQUEST // MIA - SAI"}
    values.update(changes)
    return values


def case(path, case_id):
    conn = db.open_db(path)
    try:
        return dict(conn.execute("SELECT * FROM enquiry WHERE id = ?", (case_id,)).fetchone())
    finally:
        conn.close()


# --------------------------------------------------- B: gaps don't block --
# test_a_gap_is_saved_and_named: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# test_a_bad_container_qty_writes_nothing_and_shows_the_banner: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


def test_a_refused_save_adds_no_location(world):
    client, path, ids = world
    client.post("/enquiries/{}".format(ids["dap"]),
                data=form_of(container_qty="0", new_dest_loc="VALPARAISO"))
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM location WHERE key = 'VALPARAISO'").fetchone()[0] == 0
    conn.close()


# test_an_empty_requester_is_named_not_blanked: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


def test_the_register_form_keeps_its_rules(world):
    client, path, ids = world
    body = client.post("/enquiries/new", data=dict(form_of(), decision="new")).data.decode("utf-8")
    assert "DAP needs the delivery place" in body
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == 2
    conn.close()


# ------------------------------------------------ A: the JSON save first --
# test_the_save_first_answer_is_json: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# test_the_page_script_saves_first_and_asks_before_leaving: replaced in 15a by tests/test_case_v3.py (case page v3).


# ------------------------------------------------------ C: subjects ------
def attach(path, case_id, subject, voided=(False, False)):
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    for (kind, value), void in zip((("conversation", "CONV-" + subject), ("thread_key", "KEY" + subject)), voided):
        rid = repo.attach_thread(case_id, kind, value, TODAY, note=subject)
        if void:
            repo.void_thread(rid)
    repo.commit()
    ids = [r[0] for r in conn.execute("SELECT id FROM enquiry_thread WHERE enquiry_id = ? AND note = ? ORDER BY id",
                                      (case_id, subject))]
    conn.close()
    return ids


def subjects_block(body):
    return body[body.index('id="subjects"'):body.index('class="add-subject"')]


def more_menu(body):
    menu = body[body.index('<details class="more-menu">'):]
    return menu[:menu.index("</details>")]


def test_one_line_per_subject_strike_out_and_put_back(world):
    client, path, ids = world
    rows = attach(path, ids["dap"], "RE: RATE REQUEST // MIA - SAI // REVISED")
    body = client.get("/enquiries/{}".format(ids["dap"])).data.decode("utf-8")
    block = subjects_block(body)
    assert block.count("RE: RATE REQUEST // MIA - SAI // REVISED") == 1
    form = 'id="subject-line-{}"'.format("-".join(map(str, rows)))
    assert form in body and 'name="ids" value="{}"'.format(",".join(map(str, rows))) in body
    # Strike out voids both rows and the line leaves the block
    client.post("/enquiries/{}/subjects/strike".format(ids["dap"]),
                data={"ids": ",".join(map(str, rows)), "action": "void"}, follow_redirects=True)
    conn = db.open_db(path)
    assert [r[0] for r in conn.execute("SELECT voided FROM enquiry_thread ORDER BY id")] == [1, 1]
    conn.close()
    body = client.get("/enquiries/{}".format(ids["dap"])).data.decode("utf-8")
    assert "// REVISED" not in subjects_block(body)
    assert "Put back subject: RE: RATE REQUEST // MIA - SAI // REVISED" in more_menu(body)
    # the More item puts both back
    client.post("/enquiries/{}/subjects/strike".format(ids["dap"]),
                data={"ids": ",".join(map(str, rows)), "action": "unvoid"}, follow_redirects=True)
    conn = db.open_db(path)
    assert [r[0] for r in conn.execute("SELECT voided FROM enquiry_thread ORDER BY id")] == [0, 0]
    conn.close()
    body = client.get("/enquiries/{}".format(ids["dap"])).data.decode("utf-8")
    assert "// REVISED" in subjects_block(body) and "Put back subject" not in more_menu(body)


def test_opp_0298s_shape_one_live_one_struck_is_one_live_line(world):
    client, path, ids = world
    attach(path, ids["dap"], "FW: DAP // MIA - SAI", voided=(False, True))
    body = client.get("/enquiries/{}".format(ids["dap"])).data.decode("utf-8")
    assert subjects_block(body).count("FW: DAP // MIA - SAI") == 1
    assert "Put back subject" not in more_menu(body)


def test_no_registered_line_without_a_source_subject(world):
    client, path, ids = world
    block = subjects_block(client.get("/enquiries/{}".format(ids["bare"])).data.decode("utf-8"))
    assert ">registered<" not in block and "<span class=\"note\">-</span>" not in block
    block = subjects_block(client.get("/enquiries/{}".format(ids["dap"])).data.decode("utf-8"))
    assert ">registered<" in block
