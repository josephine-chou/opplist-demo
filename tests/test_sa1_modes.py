"""Batch SA1 (schema v48): two modes of their own, Sea-Air (a real sea-air
move; Multi-mode keeps meaning 'quote air and ocean side by side') and
Cross-border Truck (a truck that itself crosses a border; Truck is trucking
inside one country), on cases and on shipments.

Every company, place, date and number here is invented; cases from
OPP-9201.
"""

from __future__ import annotations

import re
import sqlite3

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import ack, ack_questions, cargo, db, export_xlsx, intake, monthly, outlook_bridge, parsing, paths, \
    routes, schema_migrations, shipments
from opplist.web import create_app, forms

MONTH = "2027-03"
SAVE_FIRST = {"X-Save-First": "1", "Accept": "application/json"}


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "sa1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    p = add_party(repo, "Quillon Freight", country="Brazil")
    yield conn, repo, p, path
    conn.close()


def case(repo, opp, party, mode, origin="China", dest="Brazil", **extra):
    got = add_enquiry(repo, opp, party, received="2027-03-04", service_mode=mode, origin_country=origin,
                      dest_country=dest, **extra)
    repo.commit()
    return got


def mode_of(conn, table, row_id):
    return conn.execute("SELECT service_mode FROM {} WHERE id = ?".format(table), (row_id,)).fetchone()[0]


def route_a(conn, enquiry_id):
    return conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND seq = 1", (enquiry_id,)).fetchone()[0]


# ---------------------------------------------------------- the upgrade --
_V47_MODES = "'Air','LCL','FCL','RORO','Break Bulk','Project','Truck','Multi-mode','Not specified'"
_V47_KINDS = "'ocean','air','truck'"


def _back_to_v47(path):
    """enquiry, enquiry_route and shipment as v47 had them: no Sea-Air, no
    Cross-border Truck, three shipment kinds."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    schema_sql = re.sub(r"--[^\n]*", "", paths.SCHEMA_SQL.read_text(encoding="utf-8"))
    for name in re.findall(r"CREATE VIEW\s+(\w+)\s+AS", schema_sql):
        conn.execute("DROP VIEW IF EXISTS {}".format(name))
    indexes = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
                                          "AND tbl_name IN ('enquiry','enquiry_route','shipment')")]
    for table in ("enquiry", "enquiry_route", "shipment"):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
        old = (sql.replace("'Air','Sea-Air','LCL','FCL','RORO','Break Bulk','Project','Truck','Cross-border Truck',"
                           "'Multi-mode','Not specified'", _V47_MODES)
                  .replace("'ocean','air','truck','sea_air','cross_border_truck'", _V47_KINDS))
        assert old != sql
        conn.execute(old.replace("CREATE TABLE {}".format(table), "CREATE TABLE {}_old".format(table), 1))
        conn.execute("INSERT INTO {0}_old SELECT * FROM {0}".format(table))
        conn.execute("DROP TABLE {}".format(table))
        conn.execute("ALTER TABLE {0}_old RENAME TO {0}".format(table))
    for statement in indexes:
        conn.execute(statement)
    for statement in re.findall(r"CREATE VIEW\s+\w+\s+AS.*?;", schema_sql, re.S):
        conn.execute(statement)
    conn.execute("UPDATE setting SET value = '47' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()


def _snapshot(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                         "AND name NOT LIKE 'sqlite_%'")]
    views = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")]
    counts = {t: conn.execute("SELECT COUNT(*) FROM [{}]".format(t)).fetchone()[0] for t in tables}
    rows = {v: sorted(repr(tuple(r)) for r in conn.execute("SELECT * FROM [{}]".format(v))) for v in views}
    shape = {t: [tuple(r) for r in conn.execute("PRAGMA table_info([{}])".format(t))]
             + [tuple(r) for r in conn.execute("PRAGMA foreign_key_list([{}])".format(t))]
             for t in ("enquiry", "enquiry_route", "shipment")}
    indexes = sorted(tuple(r) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name IN ('enquiry','enquiry_route','shipment')"))
    return counts, rows, shape, indexes


def test_the_upgrade_from_v47_widens_three_checks_and_keeps_every_row_and_view(world):
    conn, repo, p, path = world
    fcl = case(repo, "OPP-9201", p, "FCL")
    truck = case(repo, "OPP-9202", p, "Truck", origin="USA", dest="USA")
    won = case(repo, "OPP-9203", p, "Air")
    repo.set_status(won, "Won", "2027-03-10", rate_used="own")
    shipments.start(repo, won, "Vellmar to Orrin")
    repo.commit()
    conn.close()
    _back_to_v47(path)
    raw = sqlite3.connect(path)
    before = _snapshot(raw)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE enquiry SET service_mode = 'Sea-Air' WHERE id = ?", (fcl,))
    raw.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    try:
        # SC1: the upgrade runs on to the current version (49)
        assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
        assert conn.execute("SELECT value FROM setting WHERE key = 'schema_version'").fetchone()[0] == \
            str(schema_migrations.CURRENT_VERSION)
        assert any(n.startswith("rebuilt (enquiry 3 rows, enquiry_route 3 rows, shipment 1 rows)") for n in notes)
        assert _snapshot(conn) == before                       # rows, views, columns, keys, indexes
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert mode_of(conn, "enquiry", truck) == "Truck"
        for table, mark in (("enquiry", "'Sea-Air'"), ("enquiry_route", "'Cross-border Truck'"),
                            ("shipment", "'cross_border_truck'")):
            assert mark in conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        # safe to run twice: nothing rebuilt, nothing written
        last = conn.execute("SELECT MAX(id) FROM change_log").fetchone()[0]
        assert schema_migrations._to_v48(conn)[0] == "the mode CHECKs already allow Sea-Air and Cross-border Truck"
        assert conn.execute("SELECT MAX(id) FROM change_log").fetchone()[0] == last
    finally:
        conn.close()


def test_the_upgrade_sets_the_confirmed_modes_only_while_they_are_what_she_corrected(world, monkeypatch):
    conn, repo, p, path = world
    multi = case(repo, "OPP-9204", p, "Multi-mode")
    truck = case(repo, "OPP-9205", p, "Truck", origin="USA", dest="Mexico") if False else \
        case(repo, "OPP-9205", p, "Truck", origin="USA", dest="Brazil")
    moved = case(repo, "OPP-9206", p, "LCL")                   # changed since: left alone
    already = case(repo, "OPP-9207", p, "RORO")
    conn.close()
    monkeypatch.setattr(schema_migrations, "_V48_CASES", (
        ("OPP-9204", "Multi-mode", "Sea-Air"), ("OPP-9205", "Truck", "Cross-border Truck"),
        ("OPP-9206", "Truck", "Air"), ("OPP-9207", "Truck", "RORO"), ("OPP-9299", "Truck", "Air")))
    _back_to_v47(path)
    raw = sqlite3.connect(path)
    last = raw.execute("SELECT MAX(id) FROM change_log").fetchone()[0]
    raw.close()
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    try:
        assert [mode_of(conn, "enquiry", i) for i in (multi, truck, moved, already)] == \
            ["Sea-Air", "Cross-border Truck", "LCL", "RORO"]
        assert [mode_of(conn, "enquiry_route", route_a(conn, i)) for i in (multi, truck, moved, already)] == \
            ["Sea-Air", "Cross-border Truck", "LCL", "RORO"]
        logged = conn.execute("SELECT table_name, field, old_value, new_value, source, action_id FROM change_log "
                              "WHERE id > ? ORDER BY id", (last,)).fetchall()
        assert [tuple(r)[:5] for r in logged] == [
            ("enquiry", "service_mode", "Multi-mode", "Sea-Air", "migration"),
            ("enquiry_route", "service_mode", "Multi-mode", "Sea-Air", "migration"),
            ("enquiry", "service_mode", "Truck", "Cross-border Truck", "migration"),
            ("enquiry_route", "service_mode", "Truck", "Cross-border Truck", "migration")]
        assert len({r["action_id"] for r in logged}) == 1 and logged[0]["action_id"] is not None
        assert "modes set: OPP-9204 Multi-mode -> Sea-Air; OPP-9205 Truck -> Cross-border Truck" in notes
        assert "modes already right: OPP-9207" in notes
        assert "left as they are (changed since 9 Nov): OPP-9206 (LCL, not Truck)" in notes
    finally:
        conn.close()


# --------------------------------------------------------- the mode list --
def test_the_mode_list_has_each_new_mode_after_its_neighbour():
    want = ["Air", "Sea-Air", "LCL", "FCL", "RORO", "Break Bulk", "Project", "Truck", "Cross-border Truck",
            "Multi-mode", "Not specified"]
    assert forms.SERVICE_MODES == want and export_xlsx.SERVICE_MODES == want
    assert "Cross-border Truck" in forms.FREE_TEXT_QTY_MODES and "Sea-Air" not in forms.FREE_TEXT_QTY_MODES
    assert "Cross-border Truck" in intake._SURFACE_MODES and "Sea-Air" not in intake._SURFACE_MODES


def test_the_xlsx_validation_list_carries_both_modes(world, tmp_path):
    conn, repo, p, _ = world
    case(repo, "OPP-9208", p, "Sea-Air")
    book = openpyxl.load_workbook(export_xlsx.export(conn, tmp_path / "out.xlsx"))
    sheet = book["Lists"]
    column = export_xlsx.LIST_COLUMNS.index("Service Mode") + 1
    listed = [sheet.cell(row=r, column=column).value for r in range(2, sheet.max_row + 1)]
    assert [m for m in listed if m] == forms.SERVICE_MODES
    opps = book["Opportunities"]
    head = [c.value for c in opps[1]]
    assert len(head) == 41
    assert [r[head.index("Service Mode")] for r in opps.iter_rows(min_row=2, values_only=True)] == ["Sea-Air"]


@pytest.fixture
def site(world):
    conn, repo, p, path = world
    ids = {"sea": case(repo, "OPP-9209", p, "Air", origin_loc="NGB", dest_loc="SAI"),
           "road": case(repo, "OPP-9210", p, "Truck", origin="USA", dest="Brazil", origin_loc="LAX",
                        dest_loc="SAI")}
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), conn, ids


def test_both_modes_save_from_the_case_form(site):
    client, conn, ids = site
    body = client.get("/enquiries/{}".format(ids["sea"])).data.decode("utf-8")
    options = [o for o in re.findall(r'<option value="([^"]*)"', body[body.index('name="service_mode"'):]) if o][:11]
    assert options == forms.SERVICE_MODES
    for got, mode in ((ids["sea"], "Sea-Air"), (ids["road"], "Cross-border Truck")):
        answer = client.post("/enquiries/{}".format(got), data={"service_mode": mode}, headers=SAVE_FIRST)
        assert answer.status_code == 200, answer.data
        conn.rollback()
        assert mode_of(conn, "enquiry", got) == mode and mode_of(conn, "enquiry_route", route_a(conn, got)) == mode


def test_both_modes_save_from_the_route_editor(site):
    client, conn, ids = site
    for got, mode, origin, dest in ((ids["sea"], "Sea-Air", ("China", "NGB"), ("Chile", "SAI")),
                                    (ids["road"], "Cross-border Truck", ("USA", "LAX"), ("Chile", "SAI"))):
        client.post("/enquiries/{}/routes".format(got), data={
            "form": "route", "route_id": str(route_a(conn, got)), "origin_country": origin[0],
            "origin_loc": origin[1], "dest_country": dest[0], "dest_loc": dest[1], "service_mode": mode},
            follow_redirects=True)
        conn.rollback()
        assert mode_of(conn, "enquiry_route", route_a(conn, got)) == mode and mode_of(conn, "enquiry", got) == mode
    added = client.post("/enquiries/{}/routes".format(ids["sea"]), data={
        "form": "route", "route_id": "", "origin_country": "China", "origin_loc": "NGB", "dest_country": "USA",
        "dest_loc": "MIA", "service_mode": "Cross-border Truck"}, follow_redirects=True)
    assert added.status_code == 200
    conn.rollback()
    assert conn.execute("SELECT service_mode FROM enquiry_route WHERE enquiry_id = ? AND seq = 2",
                        (ids["sea"],)).fetchone()[0] == "Cross-border Truck"


def test_a_sea_air_case_takes_the_air_fields_and_its_chargeable_weight(site):
    client, conn, ids = site
    assert forms.field_state("Sea-Air", "")["air_weight_kg"] == "required"
    assert forms.field_state("Cross-border Truck", "")["qty_raw"] == "required"
    assert forms.missing({"requester_party_id": 1, "received_on": "2027-03-04", "service_mode": "Sea-Air"}) \
        == {"air_weight_kg": "required for Sea-Air"}
    repo = db.Repository(conn, source="test")
    repo.update("enquiry", ids["sea"], {"service_mode": "Sea-Air", "gross_kg": 120, "volume_cbm": 1.5})
    repo.commit()
    row = conn.execute("SELECT * FROM enquiry WHERE id = ?", (ids["sea"],)).fetchone()
    assert routes.totals(conn, ids["sea"], case=row)["air_weight_kg"] == pytest.approx(250.0)  # 1.5 cbm beats 120 kg
    repo.update("enquiry", ids["sea"], {"service_mode": "LCL"})
    row = conn.execute("SELECT * FROM enquiry WHERE id = ?", (ids["sea"],)).fetchone()
    assert routes.totals(conn, ids["sea"], case=row)["air_weight_kg"] is None
    conn.rollback()
    assert cargo.summary(conn, ids["sea"], "Sea-Air")["is_air"]
    assert not cargo.summary(conn, ids["sea"], "Cross-border Truck")["is_air"]
    assert conn.execute("SELECT teu FROM v_enquiry WHERE id = ?", (ids["sea"],)).fetchone()[0] in (None, 0)


# ----------------------------------------------------------------- words --
def test_the_acknowledgment_questions(world):
    # FX4: the acknowledgment's words (ack.request_phrase) went with its draft; its questions stay
    assert not hasattr(ack, "request_phrase")
    for incoterm in ("EXW", "FOB"):
        assert ack_questions.standard("Sea-Air", "", incoterm) == ack_questions.standard("Air", "", incoterm)
        assert ack_questions.standard("Cross-border Truck", "", incoterm) == \
            ack_questions.standard("Truck", "", incoterm)


# ----------------------------------------------------------------- hints --
@pytest.mark.parametrize("subject", ["RATE REQUEST // SEA-AIR // VELLMAR - ORRIN", "SEA AIR QUOTE // NGB - SAI",
                                     "SEAAIR RATES FOR 3 PALLETS", "SEA/AIR // NGB - SAI"])
def test_the_subject_suggests_sea_air_ahead_of_air(subject):
    assert parsing.parse_subject(subject).service_mode == "Sea-Air"


@pytest.mark.parametrize("subject", ["QUOTE // SEA & AIR // NGB - SAI", "AIR & SEA RATES", "SEA AND AIR QUOTE",
                                     "AIR AND SEA // NGB - SAI", "AIR AND LCL RATES"])
def test_both_modes_asked_side_by_side_are_never_sea_air(subject):
    assert parsing.parse_subject(subject).service_mode != "Sea-Air"


@pytest.mark.parametrize("subject", ["CROSS BORDER // LAREDO - MONTERREY", "CROSS-BORDER TRUCKING RATES",
                                     "CROSSBORDER FTL QUOTE"])
def test_the_subject_suggests_cross_border_truck(subject):
    assert parsing.parse_subject(subject).service_mode == "Cross-border Truck"


def test_two_countries_alone_suggest_no_mode():
    assert parsing.parse_subject("RATE REQUEST // SHENZHEN, CHINA - CURITIBA, BRAZIL").service_mode is None
    assert parsing.body_mode(["From Shenzhen, China to Curitiba, Brazil, 4 pallets."]) == ("", "")


def test_a_subject_that_says_ipi_gets_no_truck_mode_and_its_boxes_decide():
    got = parsing.parse_subject("IPI // TRUCKING // NGB - DENVER // 2X40HQ")
    assert got.service_mode is None and not any(m in parsing.TRUCK_MODES for _, m in got.modes)
    got = parsing.parse_subject("IPI CROSS BORDER // LCL // NGB - MONTERREY")
    assert got.service_mode == "LCL"


@pytest.mark.parametrize("lines, want", [
    (["Could you quote sea-air for 6 cartons?"], "Sea-Air"),
    (["We would like a sea air option, ocean freight to Dubai then by air."], "Sea-Air"),
    (["Please send Seaair and sea/air both"], "Sea-Air"),
    (["Please quote a cross border truck from Laredo to Monterrey."], "Cross-border Truck"),
    (["Please quote sea and air for the same cargo", "Ocean freight and air freight both"], ""),
    (["Rates for air & sea please"], ""),
    (["IPI to Denver, cross border delivery after", "2 x 40HQ FCL"], "FCL"),
    (["IPI rail to Chicago then trucking", "LCL 4 cbm"], "LCL"),
])
def test_the_body_hints(lines, want):
    assert parsing.body_mode(lines)[0] == want


def a_mail(subject, body):
    return outlook_bridge.MailInfo(subject=subject, received_on="2027-03-04", received_at="2027-03-04 09:20",
                                   sender_address="ops@quillon.test", sender_name="Ana Velk", sender_internal=False,
                                   to_addresses=["desk@jojofreight.example"], body_excerpt=body, entry_id="ENTRY-9201",
                                   conversation_id="CONV-9201", attachment_names=[])


def test_a_mail_whose_body_says_ipi_takes_no_truck_mode_from_its_subject(world):
    conn, repo, p, _ = world
    draft = intake.build(conn, a_mail("TRUCKING // NGB - DENVER", "Hello,\nIPI to Denver, 2 x 40HQ FCL.\n"))
    assert draft.values.get("service_mode") == "FCL"
    assert any("IPI" in n and "not Truck" in n for n in draft.notes)
    draft = intake.build(conn, a_mail("TRUCKING // LAX - DENVER", "Hello,\nOne truck load please.\n"))
    assert draft.values.get("service_mode") == "Truck"


# ------------------------------------------------------------ shipments --
def test_each_new_mode_is_a_shipment_kind_of_its_own():
    assert shipments.MILESTONES["sea_air"] == ("Booked", "Picked up", "Departed by sea", "At hub",
                                               "Departed by air", "Arrived", "Delivered", "Invoiced")
    assert shipments.MILESTONES["cross_border_truck"] == ("Booked", "Dispatched", "Picked up", "At border",
                                                          "Crossed", "Delivered", "POD", "Invoiced")
    assert [shipments.mode_for(m) for m in ("Sea-Air", "Cross-border Truck", "Air", "Truck", "Multi-mode")] == \
        ["sea_air", "cross_border_truck", "air", "truck", "ocean"]


@pytest.mark.parametrize("mode, kind, label", [("Sea-Air", "sea_air", "Sea-Air"),
                                               ("Cross-border Truck", "cross_border_truck", "Cross-border Truck")])
def test_a_won_case_of_each_new_mode_starts_a_shipment_of_its_kind(world, mode, kind, label):
    conn, repo, p, _ = world
    got = case(repo, "OPP-9213", p, mode)
    repo.set_status(got, "Won", "2027-03-10", rate_used="own")
    sid = shipments.start(repo, got, "Vellmar to Orrin")
    repo.commit()
    assert conn.execute("SELECT mode FROM shipment WHERE id = ?", (sid,)).fetchone()[0] == kind
    state = shipments.summary(conn, sid, today="2027-03-12")
    assert state["milestones"] == shipments.MILESTONES[kind]
    assert [name for name, _, _ in shipments.stepper(state)] == list(shipments.MILESTONES[kind])
    assert state["pills"][0] == (label, "p-mode")
    for name in shipments.MILESTONES[kind]:                 # every step can be set by hand
        shipments.log_milestone(repo, sid, name, "2027-03-12 09:00")
    repo.commit()
    assert shipments.summary(conn, sid, today="2027-03-12")["current"] == "Invoiced"


def test_the_new_kinds_follow_truck_or_ocean_and_air(world):
    conn, repo, p, _ = world
    road = case(repo, "OPP-9214", p, "Cross-border Truck", origin="USA", dest="Brazil")
    sea = case(repo, "OPP-9215", p, "Sea-Air")
    ids = {}
    for name, got in (("road", road), ("sea", sea)):
        repo.set_status(got, "Won", "2027-03-10", rate_used="own")
        ids[name] = shipments.start(repo, got, "Vellmar to Orrin")
        shipments.edit(repo, ids[name], {"planned_arrival": "2027-03-20", "planned_delivery": "2027-03-22",
                                         "eta": "2027-03-25"})
    repo.commit()
    road_state = shipments.summary(conn, ids["road"], today="2027-03-12")
    sea_state = shipments.summary(conn, ids["sea"], today="2027-03-12")
    assert road_state["delayed_days"] == 3 and sea_state["delayed_days"] == 5    # delivery vs arrival
    assert [f[0] for f in shipments.facts(road_state)][:2] == ["Pickup window", "Delivery window"]
    assert [f[0] for f in shipments.facts(sea_state)][:2] == ["Picked up", "Departure"]
    shipments.log_milestone(repo, ids["sea"], "Departed by sea", "2027-03-12 09:00")
    shipments.log_milestone(repo, ids["road"], "At border", "2027-03-12 09:00")
    repo.commit()
    assert shipments.summary(conn, ids["sea"], today="2027-03-12")["status_text"] == "In transit"
    assert shipments.summary(conn, ids["road"], today="2027-03-12")["status_text"] == "At border"


class _ShipMail:
    def __init__(self, body):
        self.body_excerpt = self.body = body
        self.received_at = self.sent_at = "2027-03-12 10:00:00"
        self.message_id, self.sender_name, self.sender_address = "<m1@quillon.test>", "Ana Velk", "ops@quillon.test"
        self.subject = "Shipment update"


def test_the_mail_reading_keeps_its_words(world):
    conn, repo, p, _ = world
    got = shipments.detect(_ShipMail("Hello,\nThe cargo departed today.\nRegards"), mode="sea_air")
    assert got is not None and got.name == "Departed by sea"
    got = shipments.detect(_ShipMail("Hello,\nPOD attached.\nRegards"), mode="cross_border_truck")
    assert got is not None and got.name == "POD"
    got = shipments.detect(_ShipMail("Hello,\nPOD attached.\nRegards"), mode="sea_air")
    assert got is not None and got.name == "Delivered"
    for words in ("The truck is at the border.", "The truck crossed into Mexico.", "Cargo is at the hub."):
        found = shipments.detect(_ShipMail("Hello,\n{}\nRegards".format(words)), mode="cross_border_truck")
        assert found is None or found.name not in ("At border", "Crossed", "At hub")


# --------------------------------------------------------------- report --
def test_section_1_counts_each_new_mode_in_its_own_column(world):
    # SC1: was 'under Other'; each mode has its own column, in the month that has one
    conn, repo, p, _ = world
    case(repo, "OPP-9216", p, "Sea-Air", origin="Chile", dest="Brazil")
    case(repo, "OPP-9217", p, "Cross-border Truck", origin="Chile", dest="Brazil")
    case(repo, "OPP-9218", p, "Air", origin="Chile", dest="Brazil")
    table = monthly.by_scope(conn, MONTH)
    assert table.columns == ["Scope / requester", "Total", "Air", "Sea-Air", "FCL", "LCL", "Cross-border Truck",
                             "Won", "Lost"]
    total = [r for r in table.rows if r.label == "Total"][0]
    assert total.cells[:6] == ["3", "1", "1", "0", "0", "1"]
    assert ("1 / 0 / 2", "Air / Ocean / Other") in monthly.build(conn, MONTH).kpis
