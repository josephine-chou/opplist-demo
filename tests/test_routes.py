"""Batch 13 (2026-09-28, schema v33): one mail is one OPP, with routes and
cargo inside it. The migration, route results and the OPP status, the Won
picker, route-scoped cargo totals (the CIRCLUA shape), the Cargo card's Air
chargeable weight and LCL W/M, the export row and Routes sheet, the
Enquiries filters over any route, the dashboard's lanes per route, and the
subjects folding into the registered line."""

from __future__ import annotations

import re

import pytest

import enquiries_client
from conftest import add_enquiry, add_party, seed_reference
from opplist import dashboard, db, export_xlsx, routes, schema_migrations
from opplist.web import create_app

TODAY = "2026-09-28"


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "routes.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    repo.insert("country", {"name": "Canada", "region": "North America"})
    for key, country in (("SANTOS", "Brazil"), ("EXSHAW", "Canada"), ("BROOKFIELD", "Canada"),
                         ("BLOOMSDALE", "USA")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})
    norvane = add_party(repo, "Norvane", country="USA")
    ids = {"norvane": norvane}
    # CIRCLUA: one origin, three destinations, one cargo line for all
    ids["circ"] = add_enquiry(repo, "OPP-1476", norvane, received="2026-09-25", service_mode="FCL",
                              origin_loc="SANTOS", origin_country="Brazil", dest_loc="EXSHAW",
                              dest_country="Canada", incoterm="DAP", dest_alt="Highway 1A, Exshaw",
                              end_customer="CIRCLUA", commodity="white clay")
    repo.set_status(ids["circ"], "Quoted", "2026-09-28")
    ids["b"] = repo.add_route(ids["circ"], {"origin_loc": "SANTOS", "origin_country": "Brazil", "dest_loc": "BROOKFIELD",
                                            "dest_country": "Canada", "service_mode": "FCL", "incoterm": "DAP"})
    ids["c"] = repo.add_route(ids["circ"], {"origin_loc": "SANTOS", "origin_country": "Brazil", "dest_loc": "BLOOMSDALE",
                                            "dest_country": "USA", "service_mode": "FCL", "incoterm": "DDP"})
    ids["a"] = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (ids["circ"],)).fetchone()[0]
    repo.add_cargo_line(ids["circ"], {"pieces": 20, "pack_word": "big bags", "commodity": "white clay",
                                      "weight_value": 25, "weight_unit": "t", "container_type": "40GP",
                                      "container_qty": 1, "per_month": 2, "months": 24})
    # an Air case with several lines, one of them 'each' in lbs and in inches
    ids["air"] = add_enquiry(repo, "OPP-1435", norvane, received="2026-09-16", service_mode="Air",
                             origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.add_cargo_line(ids["air"], {"pieces": 2, "pack_word": "pallets", "dim_l": 52, "dim_w": 42, "dim_h": 47,
                                     "dims_unit": "in", "weight_value": 350, "weight_unit": "lbs",
                                     "weight_basis": "each"})
    repo.add_cargo_line(ids["air"], {"pieces": 1, "pack_word": "box", "dim_l": 20, "dim_w": 10, "dim_h": 7,
                                     "dims_unit": "in", "weight_value": 15, "weight_unit": "lbs"})
    # an LCL case
    ids["lcl"] = add_enquiry(repo, "OPP-1465", norvane, received="2026-09-23", service_mode="LCL",
                             origin_loc="MIA", origin_country="USA", dest_loc="SAI", dest_country="Chile")
    repo.add_cargo_line(ids["lcl"], {"pieces": 8, "pack_word": "pallets", "dim_l": 120, "dim_w": 100, "dim_h": 100,
                                     "dims_unit": "cm", "weight_value": 4500, "weight_unit": "kg"})
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def opened(path):
    return db.open_db(path)


# ------------------------------------------------------------- migration --
def test_the_migration_gives_every_case_route_a(tmp_path):
    path = tmp_path / "v32.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    won = add_enquiry(repo, "OPP-0001", tolvex, received="2026-09-01", service_mode="Air", origin_loc="MIA",
                      origin_country="USA", dest_loc="SAI", dest_country="Chile", incoterm="EXW",
                      air_weight_kg=120.0, lcl_cbm=2.0)
    repo.set_status(won, "Quoted", "2026-09-02")
    repo.set_status(won, "Won", "2026-09-05", rate_used="own")
    lost = add_enquiry(repo, "OPP-0002", tolvex, received="2026-09-01", service_mode="LCL", origin_loc="MIA",
                       origin_country="USA", dest_loc="SAI", dest_country="Chile", air_weight_kg=900.0, lcl_cbm=3.5)
    repo.set_status(lost, "Lost", "2026-09-06", closed_reason="Price too high")
    grouped = add_enquiry(repo, "OPP-0003", tolvex, received="2026-09-01", service_mode="FCL",
                          origin_country="USA", dest_country="Chile", dest_alt="Santiago warehouse")
    repo.update("enquiry", grouped, {"group_id": won})
    line = repo.add_cargo_line(grouped, {"pieces": 3, "weight_kg": 300, "deliver_to": "Valparaiso port"})
    same = repo.add_cargo_line(grouped, {"pieces": 1, "weight_kg": 50, "deliver_to": "santiago  WAREHOUSE"})
    repo.commit()
    before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM enquiry")}
    # back to v32's state of the data: no routes, the new values empty
    conn.execute("DELETE FROM enquiry_route")
    conn.execute("UPDATE enquiry SET gross_kg = NULL, volume_cbm = NULL")
    conn.execute("UPDATE enquiry_cargo SET dims_unit = NULL, weight_value = NULL, weight_unit = NULL, "
                 "route_ids = NULL, dim_l = NULL, dim_w = NULL, dim_h = NULL")
    conn.execute("UPDATE setting SET value = '32' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 33
    assert any(n.startswith("restore point opplist_before_schema") for n in notes), notes
    assert any(n.startswith("route A added to 3 case(s): 1 Won, 1 Lost") for n in notes), notes
    # route A equals the old lane on every case
    for case_id, old in before.items():
        a = conn.execute("SELECT * FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (case_id,)).fetchone()
        for key in routes.LANE_FIELDS:
            assert a[key] == old[key], (old["opp_no"], key)
    # results from the status
    assert conn.execute("SELECT result, result_on FROM enquiry_route WHERE enquiry_id = ? AND seq = 1",
                        (won,)).fetchone()[:] == ("Won", "2026-09-05")
    assert conn.execute("SELECT result, lost_reason FROM enquiry_route WHERE enquiry_id = ? AND seq = 1",
                        (lost,)).fetchone()[:] == ("Lost", "Price too high")
    # the request group is untouched
    assert conn.execute("SELECT group_id FROM enquiry WHERE id = ?", (grouped,)).fetchone()[0] == won
    # the Deliver to line became route B; the one delivering to the case's own place did not
    b = conn.execute("SELECT * FROM enquiry_route WHERE enquiry_id = ? AND seq = 2", (grouped,)).fetchone()
    assert b is not None and b["dest_alt"] == "Valparaiso port" and b["dest_country"] == "Chile"
    assert conn.execute("SELECT route_ids FROM enquiry_cargo WHERE id = ?", (line,)).fetchone()[0] == str(b["id"])
    assert conn.execute("SELECT route_ids FROM enquiry_cargo WHERE id = ?", (same,)).fetchone()[0] is None
    assert any("OPP-0003 line 1 -> route B: Valparaiso port" in n for n in notes), notes
    # the totals: gross and volume as they stood, Air charged, LCL by volume
    air = conn.execute("SELECT gross_kg, volume_cbm, air_weight_kg, lcl_cbm FROM enquiry WHERE id = ?", (won,)).fetchone()
    assert air[:] == (120.0, 2.0, round(2.0 * 1e6 / 6000, 3), None)
    lcl = conn.execute("SELECT gross_kg, volume_cbm, air_weight_kg, lcl_cbm FROM enquiry WHERE id = ?", (lost,)).fetchone()
    assert lcl[:] == (900.0, 3.5, None, 3.5)
    # re-runnable: a second pass adds nothing
    again = schema_migrations._to_v33(conn)
    assert any(n.startswith("route A added to 0 case(s)") for n in again), again
    conn.close()


# ---------------------------------------------------- results and status --
def test_the_status_follows_the_routes(world):
    client, path, ids = world
    conn = opened(path)
    repo = db.Repository(conn, source="test")
    status = lambda: conn.execute("SELECT status FROM enquiry WHERE id = ?", (ids["circ"],)).fetchone()[0]
    repo.edit_route(ids["b"], {"result": "Lost", "lost_reason": "Price too high"}, at=TODAY)
    assert status() == "Quoted"                                           # partial: unchanged
    notes = [r[0] for r in conn.execute("SELECT note FROM status_event WHERE enquiry_id = ? AND kind = 'note'",
                                        (ids["circ"],))]
    assert any(n.startswith("Route B BROOKFIELD: Lost") for n in notes), notes
    repo.edit_route(ids["a"], {"result": "Lost", "lost_reason": "Price too high"}, at=TODAY)
    repo.edit_route(ids["c"], {"result": "Lost", "lost_reason": "Rate too slow"}, at=TODAY)
    assert status() == "Lost"                                             # every route lost
    repo.edit_route(ids["c"], {"result": "Won"}, at=TODAY)
    assert status() == "Won"                                              # any route won
    conn.close()


# test_the_won_picker: deleted in batch FX3 (its route Start shipment depends on the shipments switch now). Replaced by tests/test_fx3_one_window.py's test_the_won_picker_and_the_route_shipment_buttons, on invented values.


# ------------------------------------------------------------------ totals --
def test_circlua_totals_per_route(world):
    client, path, ids = world
    conn = opened(path)
    row = conn.execute("SELECT gross_kg, container_qty, container_type, air_weight_kg, lcl_cbm FROM enquiry "
                       "WHERE id = ?", (ids["circ"],)).fetchone()
    assert row[:] == (75000.0, 3, "40GP", None, None)                    # 25 t and 1 x 40' on each of 3 routes
    t = routes.totals(conn, ids["circ"])
    assert (t["per_month"], t["project"], t["months"]) == (6.0, 144.0, 24.0)
    conn.close()
    body = client.get("/enquiries/{}".format(ids["circ"])).data.decode("utf-8")
    assert "EXSHAW · BROOKFIELD · BLOOMSDALE" in body            # the routes share an origin
    assert '<span class="pill muted-pill routes-n">3 routes</span>' in body
    assert "6 x 40GP / month" in body and "24 months · 144 x 40GP in all" in body


# test_the_cargo_card_air_chargeable_and_lcl_wm: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# ------------------------------------------------------------------ export --
def test_the_export_row_and_the_routes_sheet(world, tmp_path):
    client, path, ids = world
    conn = opened(path)
    repo = db.Repository(conn, source="test")
    repo.record_results(ids["circ"], "2026-10-12", [ids["a"], ids["c"]], {ids["b"]: "Price too high"}, rate_used="own")
    repo.commit()
    rows = {r[0]: r for r in export_xlsx.opportunities_rows(conn)}
    row = rows["OPP-1476"]
    assert len(row) == 41
    assert row[export_xlsx.OPPORTUNITY_COLUMNS.index("System Notes")].endswith(
        "A SANTOS to EXSHAW: Won; B SANTOS to BROOKFIELD: Lost, Price too high; C SANTOS to BLOOMSDALE: Won")
    assert row[export_xlsx.OPPORTUNITY_COLUMNS.index("Dest. Detail")] == "EXSHAW"        # route A
    air = rows["OPP-1435"]
    t = conn.execute("SELECT air_weight_kg FROM enquiry WHERE id = ?", (ids["air"],)).fetchone()[0]
    assert air[export_xlsx.OPPORTUNITY_COLUMNS.index("Air Weight (kgs)")] == t               # the chargeable weight
    out = export_xlsx.export(conn, tmp_path / "out" / "export.xlsx", db_path=str(path))
    from openpyxl import load_workbook
    book = load_workbook(out, read_only=True)
    assert book.sheetnames[:2] == ["Opportunities", "Routes"]
    sheet = [r for r in book["Routes"].iter_rows(values_only=True)]
    assert sheet[0] == tuple(export_xlsx.ROUTE_COLUMNS)
    circ = [r for r in sheet[1:] if r[0] == "OPP-1476"]
    assert [(r[1], r[6], r[12], r[14]) for r in circ] == [
        ("A", "EXSHAW", "Won", None), ("B", "BROOKFIELD", "Lost", "Price too high"), ("C", "BLOOMSDALE", "Won", None)]
    conn.close()


# --------------------------------------------------------------- elsewhere --
def test_enquiries_filters_match_any_route(world):
    client, path, ids = world
    page = enquiries_client.get(client, "/enquiries?view=all&direction=US%20Import")
    assert "OPP-1476" in page.shown                                    # route C goes to the USA
    page = enquiries_client.get(client, "/enquiries?view=all&direction=Cross-trade")
    assert "OPP-1476" in page.shown                                    # route A does not
    body = client.get("/enquiries?view=all").data.decode("utf-8")
    assert '<span class="lbl fill tone-grey plus-routes">+2</span>' in body
    row = re.search(r'<tr class="enq-row" data-id="{}".*?>'.format(ids["circ"]), body, re.S).group(0)
    assert "brookfield" in row and "bloomsdale" in row                 # the search box sees every route


def test_dashboard_lanes_count_routes(world):
    client, path, ids = world
    conn = opened(path)
    dash = dashboard.build(conn, TODAY, "month")
    lanes = {(l["origin"], l["dest"]): l["n"] for l in dash.lanes}
    assert lanes[("Brazil", "Canada")] == 2 and lanes[("Brazil", "USA")] == 1
    conn.close()


def test_register_two_lanes_make_one_opp_with_two_routes(world):
    client, path, ids = world
    client.post("/enquiries/new", data={
        "received_on": TODAY, "requester_party_id": str(ids["norvane"]), "service_mode": "FCL",
        "container_type": "40HQ", "container_qty": "1", "origin_country": "USA", "dest_country": "Chile",
        "lane": ["MIA>SAI", "LAX>SAI"], "commodity": "Pumps", "decision": "new"}, follow_redirects=True)
    conn = opened(path)
    case = conn.execute("SELECT id FROM enquiry WHERE commodity = 'Pumps'").fetchall()
    assert len(case) == 1
    lanes = [tuple(r) for r in conn.execute("SELECT seq, origin_loc, dest_loc FROM enquiry_route "
                                            "WHERE enquiry_id = ? ORDER BY seq", (case[0][0],))]
    assert lanes == [(1, "MIA", "SAI"), (2, "LAX", "SAI")]
    conn.close()


# test_a_route_is_added_struck_out_and_put_back: replaced in 15a by tests/test_case_v3.py (case page v3).


def test_a_subject_equal_to_the_registered_one_folds_into_it(world):
    client, path, ids = world
    conn = opened(path)
    repo = db.Repository(conn, source="test")
    repo.update("enquiry", ids["air"], {"source_subject": "RFQ // MIA - SCL"})
    repo.attach_thread(ids["air"], "conversation", "CONV-1", TODAY, note="RFQ  //  MIA - SCL")
    repo.attach_thread(ids["air"], "thread_key", "RFQMIASCL", TODAY, note="RFQ // MIA - SCL")
    repo.commit()
    conn.close()
    body = client.get("/enquiries/{}".format(ids["air"])).data.decode("utf-8")
    block = body[body.index('id="subjects"'):body.index('class="add-subject"')]
    assert block.count("RFQ // MIA - SCL") == 1 and ">registered<" in block
    conn = opened(path)
    assert conn.execute("SELECT COUNT(*) FROM enquiry_thread WHERE enquiry_id = ? AND voided = 0",
                        (ids["air"],)).fetchone()[0] == 2                 # still matching mail
    conn.close()


def test_a_case_written_without_route_a_gets_it_on_the_next_open(world):
    """An app still on older code writes a case with no route; the next
    open gives it route A from its own lane."""
    client, path, ids = world
    conn = opened(path)
    conn.execute("INSERT INTO enquiry (opp_no, requester_party_id, received_on, origin_country, dest_country, "
                 "service_mode) VALUES ('OPP-1999', ?, '2026-09-28', 'USA', 'Chile', 'Air')", (ids["norvane"],))
    conn.commit()
    conn.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    assert "route A added to 1 case(s) that had none" in notes
    row = conn.execute("SELECT r.seq, r.origin_country, r.dest_country, r.service_mode FROM enquiry_route r "
                       "JOIN enquiry e ON e.id = r.enquiry_id WHERE e.opp_no = 'OPP-1999'").fetchone()
    assert tuple(row) == (1, "USA", "Chile", "Air")
    conn.close()


def test_the_route_and_cargo_cards_are_styled_and_the_empty_forms_take_no_space():
    """The screenshots of 2026-09-28: the cards had no CSS, and each empty
    form the rows post through added a gap to the page's column."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    for rule in (".tbl.routes", ".route-editor", ".cargo-totals", ".tbl.cargo.cargo-case td .w",
                 '.case-page form[id^="route-"]', '.case-page form[id^="cargo-line-"]'):
        assert rule in css, rule
