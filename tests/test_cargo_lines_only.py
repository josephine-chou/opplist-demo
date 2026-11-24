"""Batch 15b: cargo is lines only. The migration makes one line per case from
its typed numbers (checked before writing), the case's numbers and commodity
come from the counted lines, the Cargo card shows rows and totals by mode
with units beside every number, and three new checks.

Every company, case, place, number, mail and note here is invented."""

from __future__ import annotations

import re
import sqlite3

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import cargo, cargo_card, cargo_checks, cargo_lines_migration, db, parsing, routes
from opplist.web import create_app

TABLE_MAIL = """Dear team,
Please quote air freight, EXW.
Gross Weight : 180 Kg
Volume : 1.5 CBM
Commodity : resin pellets
Packing Dimension
Packing Type
Length
Width
Height
Qty
Measurement Unit
PLT
36.00
44.00
30.00
1
INCH
Best regards"""


def case_row(conn, enquiry_id):
    return conn.execute("SELECT * FROM enquiry WHERE id = ?", (enquiry_id,)).fetchone()


@pytest.fixture
def typed(tmp_path):
    """A file of cases with typed numbers only, the way 465 cases are today."""
    path = tmp_path / "typed.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    kestrel = add_party(repo, "Kestrel Forwarding", country="Brazil")
    ids = {}
    ids["air"] = add_enquiry(repo, "OPP-2701", kestrel, received="2026-11-02", service_mode="Air",
                             commodity="resin pellets", gross_kg=410.5, volume_cbm=2.4, air_weight_kg=410.5)
    ids["lcl"] = add_enquiry(repo, "OPP-2702", kestrel, received="2026-11-02", service_mode="LCL",
                             commodity="ceramic tiles", gross_kg=900.0, volume_cbm=3.2, lcl_cbm=3.2)
    ids["fcl"] = add_enquiry(repo, "OPP-2703", kestrel, received="2026-11-02", service_mode="FCL",
                             commodity="kaolin", gross_kg=30000.0, container_type="40HQ", container_qty=2)
    ids["roro"] = add_enquiry(repo, "OPP-2704", kestrel, received="2026-11-02", service_mode="RORO",
                              container_type="Not specified")
    ids["bare"] = add_enquiry(repo, "OPP-2705", kestrel, received="2026-11-02", service_mode="Air",
                              commodity="spare parts")
    ids["lined"] = add_enquiry(repo, "OPP-2706", kestrel, received="2026-11-02", service_mode="Air",
                               commodity="brass fittings")
    repo.add_cargo_line(ids["lined"], {"pieces": 2, "pack_word": "crates", "weight_value": 120, "weight_unit": "kg"})
    # put the case's commodity back as typed: the line names none yet
    repo.update("enquiry", ids["lined"], {"commodity": "brass fittings"})
    repo.commit()
    conn.close()
    return path, ids


# ------------------------------------------------------------ migration ---
def test_the_migration_makes_one_line_per_case_with_numbers_and_changes_nothing(typed, tmp_path):
    path, ids = typed
    conn = db.open_db(path)
    before = {k: tuple(case_row(conn, v)[f] for f in cargo_lines_migration.FIELDS) for k, v in ids.items()}
    conn.close()
    report = cargo_lines_migration.run(path, write=True, restore_dir=tmp_path / "rp")
    assert report.ok and report.written, report.lines()
    assert dict(report.created) == {"Air": 1, "LCL": 1, "FCL": 1, "RORO": 1}
    assert report.without_lines == ["OPP-2705"]
    assert report.first_commodity == [("OPP-2706", "brass fittings")]
    assert report.export_differences == [] and report.differences == []
    assert list((tmp_path / "rp").glob("opplist_before_migrate-cargo-lines_*.db"))
    conn = db.open_db(path)
    for key in ("air", "lcl", "fcl", "roro", "bare", "lined"):
        now = tuple(case_row(conn, ids[key])[f] for f in cargo_lines_migration.FIELDS)
        assert now == before[key], key
    line = dict(conn.execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ?", (ids["fcl"],)).fetchone())
    assert (line["source"], line["weight_value"], line["weight_unit"], line["weight_basis"], line["container_qty"],
            line["container_type"], line["commodity"], line["counted"], line["route_ids"]) == (
        "migration", 30000.0, "kg", "total", 2, "40HQ", "kaolin", 1, None)
    air = dict(conn.execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ?", (ids["air"],)).fetchone())
    assert (air["cbm_value"], air["cbm"]) == (2.4, 2.4)
    assert conn.execute("SELECT COUNT(*) FROM enquiry_cargo WHERE enquiry_id = ?", (ids["bare"],)).fetchone()[0] == 0
    assert conn.execute("SELECT commodity FROM enquiry_cargo WHERE enquiry_id = ?", (ids["lined"],)).fetchone()[0] == "brass fittings"
    changes = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    conn.close()
    # a rerun finds nothing and writes nothing
    again = cargo_lines_migration.run(path, write=True, restore_dir=tmp_path / "rp2")
    assert sum(again.created.values()) == 0 and again.first_commodity == []
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == changes
    conn.close()


def test_a_case_whose_numbers_would_change_stops_the_migration(typed, tmp_path):
    path, ids = typed
    conn = db.open_db(path)
    conn.execute("UPDATE enquiry SET air_weight_kg = 999.0 WHERE id = ?", (ids["air"],))
    conn.commit()
    conn.close()
    report = cargo_lines_migration.run(path, write=True, restore_dir=tmp_path / "rp")
    assert not report.ok and not report.written
    assert ("OPP-2701", "air_weight_kg", 999.0, 410.5) in report.differences
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM enquiry_cargo WHERE source = 'migration'").fetchone()[0] == 0
    conn.close()
    assert not (tmp_path / "rp").exists() or not list((tmp_path / "rp").glob("*.db"))


def test_the_unrounded_v33_weight_is_kept(repo):
    seed_reference(repo)
    p = add_party(repo, "Kestrel Forwarding", country="Brazil")
    e = add_enquiry(repo, "OPP-2707", p, received="2026-11-02", service_mode="Air", gross_kg=61.24, air_weight_kg=61.237)
    line = cargo_lines_migration._line_for(case_row(repo.conn, e))
    assert line["weight_value"] == 61.237


def test_schema_v35_lets_a_line_come_from_the_migration_and_weigh_per_container(repo):
    seed_reference(repo)
    p = add_party(repo, "Kestrel Forwarding", country="Brazil")
    e = add_enquiry(repo, "OPP-2708", p, received="2026-11-02", service_mode="FCL")
    lid = repo.add_cargo_line(e, {"weight_value": 22, "weight_unit": "t", "weight_basis": "container",
                                  "container_qty": 2, "container_type": "40HQ", "source": "migration"})
    row = repo.conn.execute("SELECT source, weight_kg FROM enquiry_cargo WHERE id = ?", (lid,)).fetchone()
    assert tuple(row) == ("migration", 44000.0)
    assert db.get_setting(repo.conn, "us_chassis_max_kg") == "17200"


# ---------------------------------------------------- the derived fields ---
def test_the_case_numbers_and_commodity_follow_the_counted_lines(repo):
    seed_reference(repo)
    p = add_party(repo, "Kestrel Forwarding", country="Brazil")
    e = add_enquiry(repo, "OPP-2709", p, received="2026-11-02", service_mode="FCL", commodity="typed once")
    b = repo.add_route(e, {"origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile", "dest_loc": "SAI",
                           "service_mode": "FCL"})
    first = repo.add_cargo_line(e, {"commodity": "kaolin", "container_qty": 1, "container_type": "40GP",
                                    "weight_value": 25, "weight_unit": "t", "weight_basis": "container"})
    repo.add_cargo_line(e, {"commodity": "bentonite", "container_qty": 1, "container_type": "40GP",
                            "weight_value": 20, "weight_unit": "t", "weight_basis": "container"})
    repo.add_cargo_line(e, {"commodity": "kaolin", "container_qty": 1, "container_type": "40GP"})
    row = case_row(repo.conn, e)
    assert row["commodity"] == "kaolin; bentonite"
    assert (row["container_qty"], row["container_type"], row["gross_kg"]) == (6, "40GP", 90000.0)  # 3 a route x 2 routes
    repo.edit_cargo_line(first, {"counted": 0, "counted_present": "1"})
    row = case_row(repo.conn, e)
    assert row["commodity"] == "bentonite; kaolin" and row["container_qty"] == 4
    assert b


def test_air_chargeable_and_lcl_volume_come_from_the_lines(repo):
    seed_reference(repo)
    p = add_party(repo, "Kestrel Forwarding", country="Brazil")
    a = add_enquiry(repo, "OPP-2710", p, received="2026-11-02", service_mode="Air")
    repo.add_cargo_line(a, {"pieces": 2, "weight_value": 150, "weight_unit": "kg", "weight_basis": "each",
                            "dim_l": 120, "dim_w": 100, "dim_h": 100, "dims_unit": "cm"})
    row = case_row(repo.conn, a)
    assert (row["gross_kg"], row["volume_cbm"], row["air_weight_kg"]) == (300.0, 2.4, 400.0)
    lcl = add_enquiry(repo, "OPP-2711", p, received="2026-11-02", service_mode="LCL")
    typed_in, _ = cargo.clean({"weight_value": "1.2", "weight_unit": "t", "cbm_value": "200", "cbm_unit": "cft"})
    repo.add_cargo_line(lcl, typed_in)
    row = case_row(repo.conn, lcl)
    assert row["gross_kg"] == 1200.0 and row["lcl_cbm"] == pytest.approx(5.663, abs=0.001)


# --------------------------------------------------- units and lenient input ---
@pytest.mark.parametrize("form, field, expected", [
    ({"dim_l": "10", "dim_w": "20", "dim_h": "30", "dims_unit": "in"}, "length_cm", 25.4),
    ({"dim_l": "1000", "dim_w": "500", "dim_h": "500", "dims_unit": "mm"}, "length_cm", 100.0),
    ({"dim_l": "1.2", "dim_w": "1", "dim_h": "1", "dims_unit": "m"}, "length_cm", 120.0),
    ({"weight_value": "1,300 lbs"}, "weight_kg", 589.67),
    ({"weight_value": "25 t"}, "weight_kg", 25000.0),
    ({"cbm_value": "200", "cbm_unit": "cft"}, "cbm", 5.6634),
    ({"cbm_value": "200 cft", "cbm_unit": "cbm"}, "cbm", 5.6634),
    ({"months": "2 years"}, "months", 24.0),
    ({"weight_value": "1,300", "weight_unit": "kg"}, "weight_kg", 1300.0),
])
def test_every_box_reads_its_unit(form, field, expected):
    values, bad = cargo.clean(form)
    assert not bad
    assert cargo.normalise(values)[field] == pytest.approx(expected, abs=0.01)


def test_a_volume_typed_in_cft_is_shown_as_written():
    values, _ = cargo.clean({"cbm_value": "200", "cbm_unit": "cft"})
    line = cargo.normalise(values)
    assert line["cbm_written"] == "200 cft"
    assert cargo_card.row(line, 1, "Air", [])["volume_sub"] == "200 cft"


# --------------------------------------------------- rows and totals by mode ---
def _routes(n):
    return [{"id": i + 1, "seq": i + 1, "letter": "ABC"[i], "service_mode": "FCL"} for i in range(n)]


def test_fcl_rows_and_totals():
    line = cargo.normalise({"id": 1, "pack_word": "big bags", "commodity": "kaolin", "container_qty": 1,
                            "container_type": "40GP", "per_month": 2, "months": 24, "weight_basis": "container"})
    r = cargo_card.row(line, 1, "FCL", _routes(3))
    assert (r["what"], r["weight"], r["containers"], r["containers_sub"], r["often"], r["often_sub"], r["routes"]) == (
        "big bags", "missing", "1 x 40GP", "per shipment, each route", "2 a month", "for 24 months", "All")
    assert r["weight_missing"] and r["weight_sub"] == "per container"
    t = routes.totals(None, None, lines=[line], live_routes=_routes(3), case={"service_mode": "FCL"})
    strip = {s["label"]: (s["value"], s["formula"]) for s in cargo_card.strip(t, [line], "FCL", _routes(3))}
    assert strip["Per shipment"] == ("3 x 40GP", "1 on each of A, B, C")
    assert strip["Weight per container"] == ("-", "type the weight in line 1")
    assert strip["Per month"] == ("6 x 40GP", "2 a month x 3 routes")
    assert strip["Whole project"] == ("144 x 40GP", "6 a month x 24 months")
    weighed = cargo.normalise(dict(line, weight_value=25, weight_unit="t"))
    r = cargo_card.row(weighed, 1, "FCL", _routes(3))
    assert (r["weight"], r["weight_sub"]) == ("25 t", "= 25,000.0 kg per container")
    t = routes.totals(None, None, lines=[weighed], live_routes=_routes(3), case={"service_mode": "FCL"})
    strip = {s["label"]: s["value"] for s in cargo_card.strip(t, [weighed], "FCL", _routes(3))}
    assert strip["Weight per container"] == "25,000.0 kg" and strip["Gross per shipment"] == "75,000.0 kg"


def test_air_and_lcl_rows_and_totals():
    line = cargo.normalise({"id": 1, "pieces": 1, "pack_word": "skid", "dim_l": 40, "dim_w": 48, "dim_h": 40,
                            "dims_unit": "in", "weight_value": 1300, "weight_unit": "lbs"})
    r = cargo_card.row(line, 1, "Air", [])
    assert (r["what"], r["size"], r["size_sub"], r["weight"], r["weight_sub"]) == (
        "1 skid", "40 x 48 x 40 in", "= 101.6 x 121.9 x 101.6 cm", "1,300 lbs", "= 589.7 kg")
    assert r["volume"] == "1.259 cbm" and r["volume_sub"] == "1 x 1.259"
    t = routes.totals(None, None, lines=[line], live_routes=[{"id": 1, "seq": 1, "letter": "A", "service_mode": "Air"}],
                      case={"service_mode": "Air"})
    strip = {s["label"]: (s["value"], s["formula"]) for s in cargo_card.strip(t, [line], "Air", [])}
    assert strip["Pieces"] == ("1", "counted lines")
    assert strip["Volumetric"][0] == "209.8 kg" and strip["Chargeable"] == ("589.7 kg", "gross is higher")
    stated = cargo.normalise({"id": 2, "weight_value": 240, "weight_unit": "kg", "cbm_value": 2})
    r = cargo_card.row(stated, 1, "LCL", [])
    assert (r["what"], r["size"], r["volume_sub"], r["weight_sub"]) == ("totals only", "not given", "as stated", "in total")
    t = routes.totals(None, None, lines=[stated], live_routes=[{"id": 1, "seq": 1, "letter": "A", "service_mode": "LCL"}],
                      case={"service_mode": "LCL"})
    strip = {s["label"]: (s["value"], s["formula"]) for s in cargo_card.strip(t, [stated], "LCL", [])}
    assert strip["W/M"] == ("2.000", "max(2.000 cbm, 0.240 t)") and strip["Pieces"] == ("-", "not given")
    bare = cargo.normalise({"id": 3, "pieces": 4})
    r = cargo_card.row(bare, 1, "Air", [])
    assert r["weight"] == "missing" and r["volume"] == "missing"


# ---------------------------------------------------------- the new checks ---
def test_the_reader_reads_a_packing_table_laid_out_one_cell_per_line():
    rows = parsing.flatten_vertical_tables(TABLE_MAIL.split("\n"))
    assert "1 pallet 36.00 x 44.00 x 30.00 in" in rows and "Packing Type" not in rows
    found = cargo_card.read_lines(TABLE_MAIL)
    assert [(l.get("pieces"), l.get("dim_l"), l.get("dims_unit")) for l in found if l.get("dim_l")] == [(1, 36.0, "in")]


def test_the_size_in_the_mail_against_a_stated_volume():
    line = cargo.normalise({"id": 7, "weight_value": 180, "weight_unit": "kg", "cbm_value": 1.5})
    [check] = [c for c in cargo_checks.more_checks([line], "Air", [], TABLE_MAIL) if c.code == "mail_size"]
    assert check.kind == "ask"
    assert check.text == ("The mail also gives a size: 1 PALLET 36 x 44 x 30 in = 0.779 cbm, not the stated 1.500. "
                          "On the size the chargeable weight is 180.0 kg; on 1.5 cbm it is 250.0 kg.")
    # batch 23: the seventh part is the Packing the mail's word means
    assert check.actions == [("Use the size", "use_mail_size", "1,pallet,36,44,30,in,Pallets")]


def test_the_weight_a_month_the_mail_states():
    line = cargo.normalise({"id": 1, "container_qty": 1, "container_type": "40GP", "per_month": 2,
                            "weight_value": 20, "weight_unit": "t", "weight_basis": "container"})
    text = "Demand:\n40 toneladas por mes, por destino.\n2 containers de 40 pes por mes."
    [ok] = [c for c in cargo_checks.more_checks([line], "FCL", [], text) if c.code == "per_month"]
    assert (ok.kind, ok.text) == ("ok", "Matches the mail: 40 t a month per destination (2 x 20.0 t).")
    light = cargo.normalise(dict(line, weight_value=12))
    [ask] = [c for c in cargo_checks.more_checks([light], "FCL", [], "30 tons per month") if c.code == "per_month"]
    assert ask.kind == "ask" and "line 1 gives 24.0 t a month (2 x 12.0 t)" in ask.text


def test_us_road_weight():
    line = cargo.normalise({"id": 1, "container_qty": 1, "container_type": "40HQ", "weight_value": 22,
                            "weight_unit": "t", "weight_basis": "container"})
    door = [{"id": 1, "letter": "A", "service_mode": "FCL", "service_scope": "Port to door",
             "dest_country": "USA", "origin_country": "Chile"}]
    [check] = [c for c in cargo_checks.more_checks([line], "FCL", door, "", 17200) if c.code == "us_road"]
    assert check.text == ("Route A is delivered on US roads: 22.0 t in a 40' is over a standard chassis (17.2 t). "
                          "Ask for a tri-axle chassis or an overweight permit.")
    port = [dict(door[0], service_scope="Port to port")]
    assert not [c for c in cargo_checks.more_checks([line], "FCL", port, "", 17200) if c.code == "us_road"]
    assert not [c for c in cargo_checks.more_checks([line], "FCL", door, "", 25000) if c.code == "us_road"]


def test_a_missing_number_is_an_amber_check_and_the_rails_words():
    line = cargo.normalise({"id": 1, "container_qty": 1, "container_type": "40GP"})
    checks = cargo_checks.more_checks([line], "FCL", [], "")
    assert [(c.code, c.text) for c in checks] == [("missing", "Line 1: the weight is missing.")]
    assert cargo_checks.rail_words(checks) == "weight missing"


# ----------------------------------------------------------------- the page ---
@pytest.fixture
def web(tmp_path):
    path = tmp_path / "card.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = add_party(repo, "Kestrel Forwarding", country="Brazil")
    ids = {"party": p}
    ids["air"] = add_enquiry(repo, "OPP-2720", p, received="2026-11-02", service_mode="Air", qty_raw="Air:180")
    ids["line"] = repo.add_cargo_line(ids["air"], {"weight_value": 180, "weight_unit": "kg", "cbm_value": 1.5,
                                                   "source": "mail", "source_text": TABLE_MAIL})
    ids["empty"] = add_enquiry(repo, "OPP-2721", p, received="2026-11-02", service_mode="LCL",
                               qty_raw="LCL: 3 pallets, 2.1 cbm")
    db.set_setting(conn, "next_opp_number", 2730)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def count(path, sql, *args):
    conn = db.open_db(path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def test_no_typed_quantity_box_on_the_case_page_and_the_post_refuses_them(web):
    client, path, ids = web
    body = client.get("/enquiries/{}".format(ids["air"])).data.decode("utf-8")
    card = body[body.index('<section class="card sec-card" id="cargo">'):body.index('id="vendors"')]
    for name in ('name="gross_kg"', 'name="volume_cbm"', 'name="container_qty" value="', 'name="qty_raw"',
                 'id="parse-qty"', "Cargo details", 'name="gross_unit"'):
        assert name not in card, name
    form_names = set(re.findall(r'name="([a-z_]+)"', body[body.index('id="request"'):body.index('id="routes"')]))
    assert not form_names & {"commodity", "gross_kg", "volume_cbm", "container_type", "container_qty", "qty_raw"}
    refused = client.post("/enquiries/{}".format(ids["air"]), data={"commodity": "something else"},
                          headers={"X-Save-First": "1", "Accept": "application/json"})
    assert refused.status_code == 422 and "cargo lines" in refused.get_json()["errors"][0]["message"]
    refused = client.post("/enquiries/{}".format(ids["air"]), data={"gross_kg": "5"}, follow_redirects=True)
    assert b"come from the cargo lines" in refused.data
    assert count(path, "SELECT gross_kg FROM enquiry WHERE id = ?", ids["air"]) == 180.0


def test_the_card_shows_the_mail_text_and_its_checks(web):
    client, path, ids = web
    body = client.get("/enquiries/{}".format(ids["air"])).data.decode("utf-8")
    card = body[body.index('<section class="card sec-card" id="cargo">'):body.index('id="vendors"')]
    assert 'id="cargo-source" hidden' in card and "Show the mail text" in card and "Read into lines" in card
    assert '<span class="num">180</span> Kg' in card                         # the numbers highlighted
    assert "The mail also gives a size" in card and 'data-use-size="{}"'.format(ids["line"]) in card
    assert ">Volumetric<" in card and ">Chargeable<" in card
    # a case with no line: 'No cargo lines', the text as received labelled
    body = client.get("/enquiries/{}".format(ids["empty"])).data.decode("utf-8")
    card = body[body.index('<section class="card sec-card" id="cargo">'):body.index('id="vendors"')]
    assert "No cargo lines" in card and "Quantity as received" in card and "+ Add line" in card


def test_read_into_lines_and_the_preview_save_nothing(web):
    client, path, ids = web
    lines, logs = count(path, "SELECT COUNT(*) FROM enquiry_cargo"), count(path, "SELECT COUNT(*) FROM change_log")
    res = client.post("/enquiries/{}/cargo/read".format(ids["air"])).get_json()
    assert res["ok"] and res["n"] >= 1 and 'data-new' in res["html"] and 'name="dim_l"' in res["html"]
    res = client.post("/enquiries/{}/cargo/preview".format(ids["air"]), data={
        "line_id": ids["line"], "pieces": "1", "dim_l": "36", "dim_w": "44", "dim_h": "30", "dims_unit": "in",
        "dims_unit_present": "1", "weight_value": "180", "weight_unit": "kg", "cbm_value": "", "cbm_unit": "cbm"}).get_json()
    assert res["ok"] and "0.779 cbm" in res["html"] and "180.0 kg" in res["html"]
    assert count(path, "SELECT COUNT(*) FROM enquiry_cargo") == lines
    assert count(path, "SELECT COUNT(*) FROM change_log") == logs
    assert count(path, "SELECT cbm FROM enquiry_cargo WHERE id = ?", ids["line"]) == 1.5
    # Save line is what writes
    client.post("/enquiries/{}/cargo".format(ids["air"]), data={
        "action": "save", "line_id": ids["line"], "pieces": "1", "dim_l": "36", "dim_w": "44", "dim_h": "30",
        "dims_unit": "in", "dims_unit_present": "1", "weight_value": "180", "weight_unit": "kg", "cbm_value": "",
        "cbm_unit": "cbm"})
    assert count(path, "SELECT cbm FROM enquiry_cargo WHERE id = ?", ids["line"]) is None


def test_a_registration_s_typed_numbers_become_its_first_line(web):
    client, path, ids = web
    page = client.post("/enquiries/new", data={
        "received_on": "2026-11-04", "requester_party_id": str(ids["party"]), "service_mode": "Air",
        "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile", "dest_loc": "SAI",
        "commodity": "lab glassware", "gross_kg": "640", "gross_unit": "lbs", "volume_cbm": "1.1", "owner": "V",
        "form_token": "tok-2730", "decision": "new"})
    assert page.status_code == 302, re.findall(r'<li><strong>[^<]*</strong>[^<]*', page.data.decode("utf-8"))
    conn = db.open_db(path)
    case = conn.execute("SELECT * FROM enquiry WHERE opp_no = 'OPP-2730'").fetchone()
    [line] = conn.execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ?", (case["id"],)).fetchall()
    assert (line["weight_value"], line["weight_unit"], line["cbm_value"], line["commodity"]) == (
        pytest.approx(290.299, abs=0.01), "kg", 1.1, "lab glassware")
    assert case["commodity"] == "lab glassware" and case["gross_kg"] == pytest.approx(290.3, abs=0.01)
    conn.close()


# ----------------------------- what the replaced tests checked, still true ---
SAVE_FIRST = {"X-Save-First": "1", "Accept": "application/json"}


def test_a_line_added_and_removed_through_the_card_moves_the_totals(web):
    client, path, ids = web
    page = client.post("/enquiries/{}/cargo".format(ids["air"]), data={
        "action": "add", "pieces": "10", "pack_word": "crates", "dim_l": "88 x 60 x 42 cm", "weight_value": "3,540",
        "weight_unit": "kg", "cbm_value": "2.2", "cbm_unit": "cbm"}, follow_redirects=True)
    assert page.status_code == 200 and b"Cargo line added" in page.data
    conn = db.open_db(path)
    line = dict(conn.execute("SELECT * FROM enquiry_cargo WHERE enquiry_id = ? AND seq = 2", (ids["air"],)).fetchone())
    conn.close()
    assert (line["length_cm"], line["width_cm"], line["height_cm"], line["weight_kg"], line["source"]) == (88, 60, 42, 3540, "manual")
    assert count(path, "SELECT gross_kg FROM enquiry WHERE id = ?", ids["air"]) == 3720.0
    client.post("/enquiries/{}/cargo".format(ids["air"]), data={"action": "void", "line_id": line["id"]})
    assert count(path, "SELECT voided FROM enquiry_cargo WHERE id = ?", line["id"]) == 1
    assert count(path, "SELECT gross_kg FROM enquiry WHERE id = ?", ids["air"]) == 180.0
    body = client.get("/enquiries/{}".format(ids["air"])).data.decode("utf-8")
    assert "1 removed line(s)" in body and 'value="unvoid"' in body


def test_the_request_card_saves_handled_by_and_is_logged(web):
    client, path, ids = web
    client.post("/enquiries/{}".format(ids["air"]), data={"handled_by": "YUL"}, headers=SAVE_FIRST)
    assert count(path, "SELECT handled_by FROM enquiry WHERE id = ?", ids["air"]) == "YUL"
    assert count(path, "SELECT new_value FROM change_log WHERE table_name = 'enquiry' AND row_id = ? "
                       "AND field = 'handled_by'", ids["air"]) == "YUL"
    client.post("/enquiries/{}".format(ids["air"]), data={"handled_by": "JJC"}, headers=SAVE_FIRST)
    assert count(path, "SELECT handled_by FROM enquiry WHERE id = ?", ids["air"]) is None


def test_a_request_save_names_the_gaps_refuses_a_bad_value_and_never_blanks_the_requester(web):
    client, path, ids = web
    ok = client.post("/enquiries/{}".format(ids["air"]), data={"end_customer": "Lumen Paper SA"}, headers=SAVE_FIRST)
    assert ok.status_code == 200 and ok.get_json()["ok"] is True and ok.get_json()["message"].startswith("Saved")
    assert count(path, "SELECT new_value FROM change_log WHERE table_name = 'enquiry' AND row_id = ? "
                       "AND field = 'end_customer'", ids["air"]) == "Lumen Paper SA"
    bad = client.post("/enquiries/{}".format(ids["air"]), data={"received_on": "the 3rd", "end_customer": "Other"},
                      headers=SAVE_FIRST)
    assert bad.status_code == 422 and bad.get_json()["errors"][0]["field"] == "received_on"
    assert count(path, "SELECT end_customer FROM enquiry WHERE id = ?", ids["air"]) == "Lumen Paper SA"
    empty = client.post("/enquiries/{}".format(ids["air"]), data={"requester_party_id": ""}, headers=SAVE_FIRST)
    assert "Requester, pick who is asking" in empty.get_json()["message"]
    assert count(path, "SELECT requester_party_id FROM enquiry WHERE id = ?", ids["air"]) == ids["party"]
    conn = db.open_db(path)
    conn.execute("UPDATE enquiry SET owner = 'Ana' WHERE id = ?", (ids["air"],))
    conn.commit()
    conn.close()
    client.post("/enquiries/{}".format(ids["air"]), data={"end_customer": "Marten Tiles"}, headers=SAVE_FIRST)
    assert count(path, "SELECT owner FROM enquiry WHERE id = ?", ids["air"]) == "Ana"


def test_the_line_editor_s_numbers_are_marked_and_read_as_sentences(web):
    client, path, ids = web
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    fcl = add_enquiry(repo, "OPP-2722", ids["party"], received="2026-11-02", service_mode="FCL")
    repo.add_cargo_line(fcl, {"container_qty": 1, "container_type": "40GP", "per_month": 2, "months": 24})
    repo.commit()
    conn.close()
    body = client.get("/enquiries/{}".format(fcl)).data.decode("utf-8")
    editor = body[body.index('id="cargo-edit-'):body.index("</form>", body.index('id="cargo-edit-'))]
    for name in ("pieces", "weight_value", "container_qty", "per_month"):
        assert re.search(r'name="{}"[^>]*data-num'.format(name), editor), name
    assert "shipments a month, for" in editor and ">months<" in editor and 'value="container" selected' in editor
    body = client.get("/enquiries/{}".format(ids["air"])).data.decode("utf-8")
    editor = body[body.index('id="cargo-edit-'):body.index("</form>", body.index('id="cargo-edit-'))]
    for name in ("dim_l", "weight_value", "cbm_value"):
        assert re.search(r'name="{}"[^>]*data-num'.format(name), editor), name
    assert 'name="cbm_unit"' in editor and "each piece" in editor and 'placeholder="from size"' in editor


def test_a_registration_in_lbs_or_cft_keeps_its_numbers_on_the_line(web):
    client, path, ids = web
    base = {"received_on": "2026-11-04", "requester_party_id": str(ids["party"]), "origin_country": "USA",
            "origin_loc": "LAX", "dest_country": "China", "dest_loc": "NGB", "owner": "V", "decision": "new"}
    page = client.post("/enquiries/new", data=dict(base, service_mode="Air", gross_kg="2,200 lbs", gross_unit="kg",
                                                   volume_cbm="3", volume_unit="cbm", form_token="tok-a"))
    assert page.status_code == 302
    conn = db.open_db(path)
    case = conn.execute("SELECT * FROM enquiry WHERE opp_no = 'OPP-2730'").fetchone()
    assert (case["gross_kg"], case["volume_cbm"], case["air_weight_kg"]) == (997.9, 3.0, 997.903)
    conn.close()
    page = client.post("/enquiries/new", data=dict(base, service_mode="Air", form_token="tok-b"))
    assert page.status_code == 400 and b"Air needs a weight" in page.data
    page = client.post("/enquiries/new", data=dict(base, service_mode="LCL", volume_cbm="200", volume_unit="cft",
                                                   form_token="tok-c"))
    assert page.status_code == 302
    conn = db.open_db(path)
    case = conn.execute("SELECT * FROM enquiry WHERE opp_no = 'OPP-2731'").fetchone()
    assert case["volume_cbm"] == 5.663 and case["lcl_cbm"] == 5.663
    conn.close()


def test_air_chargeable_and_lcl_wm_on_the_page_and_dashes_when_nothing_is_typed(web):
    client, path, ids = web
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    air = add_enquiry(repo, "OPP-2723", ids["party"], received="2026-11-02", service_mode="Air")
    repo.add_cargo_line(air, {"pieces": 2, "dim_l": 52, "dim_w": 42, "dim_h": 47, "dims_unit": "in",
                              "weight_value": 350, "weight_unit": "lbs", "weight_basis": "each"})
    lcl = add_enquiry(repo, "OPP-2724", ids["party"], received="2026-11-02", service_mode="LCL")
    repo.add_cargo_line(lcl, {"pieces": 8, "dim_l": 120, "dim_w": 100, "dim_h": 100, "dims_unit": "cm",
                              "weight_value": 4500, "weight_unit": "kg"})
    bare = add_enquiry(repo, "OPP-2725", ids["party"], received="2026-11-02", service_mode="Air")
    repo.add_cargo_line(bare, {"pieces": 3, "pack_word": "drums"})
    repo.commit()
    conn.close()
    card = client.get("/enquiries/{}".format(air)).data.decode("utf-8")
    card = card[card.index('id="cargo"'):card.index('id="vendors"')]
    assert "52 x 42 x 47 in" in card and "= 132.1 x 106.7 x 119.4 cm" in card
    assert "2 x 350 lbs = 317.5 kg" in card and "2 x 1.682" in card
    assert ">Volumetric<" in card and ">Chargeable<" in card and "volumetric is higher" in card and ">W/M<" not in card
    card = client.get("/enquiries/{}".format(lcl)).data.decode("utf-8")
    card = card[card.index('id="cargo"'):card.index('id="vendors"')]
    assert ">W/M<" in card and "max(9.600 cbm, 4.500 t)" in card and ">Volumetric<" not in card
    card = client.get("/enquiries/{}".format(bare)).data.decode("utf-8")
    strip = card[card.index('class="cargo-strip'):card.index('id="cargo-checks"')]
    assert '<div class="v">-</div>' in strip and "0.0 kg" not in strip and "gross is higher" not in strip
    assert "Line 1: the weight is missing." in card


def test_the_us_chassis_limit_is_on_the_settings_page_and_saved(web):
    client, path, ids = web
    page = client.get("/settings").data.decode("utf-8")
    assert 'name="us_chassis_max_kg" value="17200"' in page
    form = {"us_chassis_max_kg": "18,000"}
    client.post("/settings", data=form)
    assert count(path, "SELECT value FROM setting WHERE key = 'us_chassis_max_kg'") == "18000"
    client.post("/settings", data={"us_chassis_max_kg": "heavy"})
    assert count(path, "SELECT value FROM setting WHERE key = 'us_chassis_max_kg'") == "18000"


# ------------------------------------------- restore points beside the db ---
def test_a_run_on_a_copy_keeps_its_restore_points_beside_the_copy(typed, tmp_path):
    from opplist import backup, paths
    from opplist.__main__ import main

    path, ids = typed
    standard = paths.RESTORE_POINTS_DIR            # the live folder, under tmp in tests
    copy_dir = tmp_path / "elsewhere"
    copy_dir.mkdir()
    copy = copy_dir / "opplist.db"
    copy.write_bytes(path.read_bytes())

    assert main(["migrate-cargo-lines", "--db", str(copy), "--apply"]) == 0
    assert len(list((copy_dir / "restore-points").glob("opplist_before_migrate-cargo-lines_*.db"))) == 1
    assert not standard.exists() or not list(standard.glob("opplist_*.db"))

    # every kind of copy follows the database it copies
    assert backup.daily(copy).parent == copy_dir / "restore-points"
    assert backup.before_task(copy, "some-task").parent == copy_dir / "restore-points"
    assert not standard.exists() or not list(standard.glob("opplist_*.db"))
    # the live database's folder is still data\restore-points
    assert backup.folder_for(paths.DATA_DIR / "opplist.db") == (paths.DATA_DIR / "restore-points").resolve()


def test_the_export_page_backup_lands_beside_the_database_it_serves(tmp_path):
    from opplist import paths

    copy_dir = tmp_path / "elsewhere"
    copy_dir.mkdir()
    path = copy_dir / "opplist.db"
    conn = db.open_db(path)
    seed_reference(db.Repository(conn, source="test"))
    conn.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    page = app.test_client().post("/export/backup", follow_redirects=True)
    assert page.status_code == 200 and b"Restore point" in page.data
    made = list((copy_dir / "restore-points").glob("opplist_before_manual_*.db"))
    assert len(made) == 1 and made[0].name.encode() in page.data
    standard = paths.RESTORE_POINTS_DIR
    assert not standard.exists() or not list(standard.glob("opplist_*.db"))
