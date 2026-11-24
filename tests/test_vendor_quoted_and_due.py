"""One vendor status, not two (2026-09-25): `quoted` follows
`machine_status` in the repository; the case page shows no quoted box.
And due now is red: the Next tile, the rail's title and the strip's rung
carry `due` when the ladder's next step has no working days left."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, fix_vendor_quoted, tracking
from opplist import web as web_module
from opplist.web import create_app

TODAY = "2026-09-23"                                          # Wednesday


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "quoted.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    seavio = add_party(repo, "Seavio", country="USA")
    craft = add_party(repo, "Vanta Multimodal", country="USA")
    # FU2 due today: quoted Mon Sep 14, FU1 Wed Sep 16, fu2_days 5 -> Wed Sep 23
    due = add_enquiry(repo, "OPP-1407", tolvex, received="2026-09-07", service_mode="FCL",
                      container_type="40GP", container_qty=1, origin_country="USA", origin_loc="MIA",
                      dest_country="Chile", dest_loc="SAI")
    repo.set_status(due, "Quoted", "2026-09-14")
    repo.add_followup(due, "2026-09-16")
    # FU2 in three working days: FU1 Mon Sep 21 -> due Mon Sep 28
    waiting = add_enquiry(repo, "OPP-1408", tolvex, received="2026-09-08", service_mode="Air",
                          origin_country="USA", dest_country="Chile")
    repo.set_status(waiting, "Quoted", "2026-09-14")
    repo.add_followup(waiting, "2026-09-21")
    repo.commit()
    return conn, path, {"due": due, "waiting": waiting, "seavio": seavio, "craft": craft}


# ------------------------------------------------------- derived flag ----
def test_quoted_is_derived_on_insert_and_update(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    row_id = repo.insert("vendor_request", {"enquiry_id": ids["due"], "vendor_party_id": ids["seavio"],
                                            "asked_on": "2026-09-14", "quoted": 1})          # the flag alone: ignored
    assert conn.execute("SELECT quoted, machine_status FROM vendor_request WHERE id = ?", (row_id,)).fetchone()[:] == (0, None)
    repo.update("vendor_request", row_id, {"machine_status": "Quoted", "quoted": 0})          # the status wins
    assert conn.execute("SELECT quoted FROM vendor_request WHERE id = ?", (row_id,)).fetchone()[0] == 1
    repo.update("vendor_request", row_id, {"their_ref": "KQRS64829"})                           # an unrelated update keeps it
    assert conn.execute("SELECT quoted FROM vendor_request WHERE id = ?", (row_id,)).fetchone()[0] == 1
    repo.update("vendor_request", row_id, {"machine_status": "Declined"})
    assert conn.execute("SELECT quoted FROM vendor_request WHERE id = ?", (row_id,)).fetchone()[0] == 0
    other = repo.insert("vendor_request", {"enquiry_id": ids["due"], "vendor_party_id": ids["craft"],
                                           "asked_on": "2026-09-14", "machine_status": "Quoted"})
    assert conn.execute("SELECT quoted FROM vendor_request WHERE id = ?", (other,)).fetchone()[0] == 1
    # every flip is in the change log
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'vendor_request' "
                        "AND field = 'quoted'").fetchone()[0] == 2
    assert conn.execute("SELECT vendor_quoted_count FROM v_enquiry WHERE id = ?", (ids["due"],)).fetchone()[0] == 1


def test_the_case_page_has_no_quoted_box_and_the_routes_derive_it(world):
    conn, path, ids = world
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    # the add row: a status select, no box
    client.post("/enquiries/{}/vendors".format(ids["due"]), data={
        "vendor_party_id": str(ids["craft"]), "asked_on": "2026-09-14", "replied_on": "2026-09-14",
        "machine_status": "Quoted", "their_ref": "KQRS64829", "quoted": "on"}, follow_redirects=True)
    body = client.get("/enquiries/{}".format(ids["due"])).data.decode("utf-8")
    assert 'name="quoted"' not in body                                         # table, edit row, add row
    vendors = body[body.index('id="vendors"'):body.index('id="mail"')]           # v2e: Rate requests sits after Route
    assert "<th>Quoted</th>" not in vendors and "Quoted to them" not in vendors
    assert 'name="machine_status"' in vendors                                  # the one status, in both forms
    assert "KQRS64829" in body[body.index('<div class="facts">'):body.index('<div class="layout">')]
    conn = db.open_db(path)
    vendor_id, quoted = conn.execute("SELECT id, quoted FROM vendor_request WHERE enquiry_id = ?",
                                     (ids["due"],)).fetchone()[:]
    assert quoted == 1
    conn.close()
    # the edit row: changing the status changes the flag, a posted box is ignored
    client.post("/enquiries/{}/vendors".format(ids["due"]), data={
        "vendor_request_id": vendor_id, "action": "save", "asked_on": "2026-09-14",
        "replied_on": "2026-09-15", "machine_status": "Declined", "quoted": "on"}, follow_redirects=True)
    conn = db.open_db(path)
    assert conn.execute("SELECT quoted, machine_status FROM vendor_request WHERE id = ?",
                        (vendor_id,)).fetchone()[:] == (0, "Declined")
    conn.close()


def test_the_backfill_counts_both_ways(world):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    # rows written the old way, straight into the table
    conn.execute("INSERT INTO vendor_request (enquiry_id, vendor_party_id, asked_on, machine_status, quoted, their_ref) "
                 "VALUES (?, ?, '2026-09-14', 'Quoted', 0, 'KQRS64829')", (ids["due"], ids["craft"]))
    conn.execute("INSERT INTO vendor_request (enquiry_id, vendor_party_id, asked_on, machine_status, quoted) "
                 "VALUES (?, ?, '2026-09-14', 'Asked', 1)", (ids["due"], ids["seavio"]))
    conn.execute("INSERT INTO vendor_request (enquiry_id, vendor_party_id, asked_on, machine_status, quoted) "
                 "VALUES (?, ?, '2026-09-15', NULL, 1)", (ids["waiting"], ids["seavio"]))
    conn.execute("INSERT INTO vendor_request (enquiry_id, vendor_party_id, asked_on, machine_status, quoted) "
                 "VALUES (?, ?, '2026-09-15', 'Declined', 0)", (ids["waiting"], ids["craft"]))
    conn.commit()
    report = fix_vendor_quoted.run(conn, dry_run=True)
    assert (report.flag_set, report.status_set) == (1, 2)
    assert conn.execute("SELECT COUNT(*) FROM vendor_request WHERE quoted = 1 AND machine_status = 'Quoted'").fetchone()[0] == 0
    report = fix_vendor_quoted.run(conn, dry_run=False)
    assert (report.flag_set, report.status_set) == (1, 2)
    assert conn.execute("SELECT COUNT(*) FROM vendor_request WHERE quoted = 1 AND machine_status = 'Quoted'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM vendor_request WHERE quoted = 0 AND machine_status = 'Declined'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE table_name = 'vendor_request' "
                        "AND source = 'fix-vendor-quoted'").fetchone()[0] == 3
    again = fix_vendor_quoted.run(conn, dry_run=False)
    assert (again.flag_set, again.status_set) == (0, 0)                         # idempotent
    assert conn.execute("SELECT vendor_quoted_count FROM v_enquiry WHERE id = ?", (ids["due"],)).fetchone()[0] == 2


# ------------------------------------------------------------ due now ----
# test_the_ladder_says_due_today_and_in_three_days: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)


# test_due_now_is_marked_in_the_three_places: replaced in L2 by tests/test_fu1_wait_rewrites.py with invented data (no FU2 without an answer to FU1)
