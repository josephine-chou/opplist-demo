"""Batch CS1: the cheat sheet counts Other as the month report does; a case
without a mode (blank or Not specified) is its own part, no mode.

Every company, place, date and number here is invented; cases from
OPP-8401.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import cheatsheet, db, monthly
from opplist.web import create_app

MONTH = "2027-03"
CLEAN = "2027-04"           # a month with no case without a mode


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "cs1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    repo.insert("country", {"name": "Mexico", "region": "Latin America"})
    p = {"wren": add_party(repo, "Wrenfield Cargo", country="Chile"),
         "pike": add_party(repo, "Pikestone Forwarding", country="USA"),
         "office": add_party(repo, "JoJo JJV", ptype="Office", country="Unknown")}
    n = [8401]
    got = {}

    def add(key, party, mode, origin="USA", dest="Chile", service=None, month=MONTH, day=3):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        values = dict(service_mode=mode, origin_country=origin, dest_country=dest)
        got[key] = add_enquiry(repo, opp, p[party], received="{}-{:02d}".format(month, day), status="Quoted",
                               **{k: v for k, v in values.items() if v is not None})
        if service:
            route = conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ?", (got[key],)).fetchone()[0]
            repo.update("enquiry_route", route, {"service_scope": service})

    add("air", "wren", "Air")
    add("fcl", "wren", "FCL")
    add("lcl1", "wren", "LCL")
    add("lcl2", "wren", "LCL")
    add("truck", "wren", "Truck", dest="Mexico")
    add("not_specified", "wren", "Not specified")
    add("blank", "pike", None, dest="China")
    add("customs_air", "pike", "Air", dest="China", service="Customs only")
    # SC1: from Mexico - an office's case from October counts when it is LATAM
    add("warehouse", "office", "Not specified", origin="Mexico", dest="USA", service="Warehousing")
    add("truck2", "office", "Truck", origin="Mexico", dest="USA")
    add("april", "wren", "Air", month=CLEAN)
    add("april_ware", "pike", "Not specified", month=CLEAN, service="Warehousing")
    repo.commit()
    yield conn, repo, got, path
    conn.close()


def reading(conn, case_id):
    return [c for c in cheatsheet._cases(conn, MONTH) if c["id"] == case_id][0]["reading"]


def cheat_page(path, month):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client().get("/report", query_string={"month": month, "view": "cheat"}).data.decode("utf-8")


def test_a_not_specified_and_a_blank_case_are_no_mode_not_other(world):
    conn, repo, got, path = world
    assert reading(conn, got["not_specified"]) == "" and reading(conn, got["blank"]) == ""
    g = cheatsheet.build(conn, MONTH).glance
    assert g["no_mode"] == 2


def test_a_customs_only_case_by_air_is_other_not_air(world):
    conn, repo, got, path = world
    assert reading(conn, got["customs_air"]) == "other"
    g = cheatsheet.build(conn, MONTH).glance
    assert g["air"] == 1


def test_the_glance_adds_up_and_its_other_is_the_report_s(world):
    conn, repo, got, path = world
    g = cheatsheet.build(conn, MONTH).glance
    assert (g["air"], g["fcl"], g["lcl"], g["other"], g["no_mode"]) == (1, 1, 2, 4, 2)
    assert g["air"] + g["fcl"] + g["lcl"] + g["other"] + g["no_mode"] == g["n"] == 10
    [kpi] = [v for v, label in monthly.kpis(conn, MONTH) if label == monthly.OTHER_LABEL]
    assert int(kpi.split(" / ")[2]) == g["other"]
    assert g["other_parts"] == "Truck 2, Customs only 1, Warehousing 1"
    html = cheat_page(path, MONTH)
    assert ("LCL 2 &middot; Other 4 (Truck 2, Customs only 1, Warehousing 1) &middot; "
            '<span class="nomode">no mode 2</span></span>') in html
    assert ".mreport .nomode{color:#9b2c3c}" in cheatsheet.CSS


def test_each_group_s_other_is_section_1_s(world):
    # SC1: section 1 has a column per mode; the sheet's Other is every one
    # of them that is not Air, FCL or LCL (here Truck and Other)
    conn, repo, got, path = world
    table = monthly.build(conn, MONTH).by_scope
    at = [i - 1 for i, c in enumerate(table.columns) if c in monthly.MODE_COLUMNS and c not in ("Air", "FCL", "LCL")]
    assert [table.columns[i + 1] for i in at] == ["Truck", "Other"]
    section1 = {}
    for row in table.rows:
        if row.cls == "total":                          # By direction follows
            break
        if row.cls == "indent":
            name = monthly.FOLD[row.label]
            section1[name] = section1.get(name, 0) + sum(int(row.cells[i]) for i in at)
    rows = {grp["name"]: grp["row"] for grp in cheatsheet.build(conn, MONTH).groups}
    assert {name: r["other"] for name, r in rows.items()} == section1
    assert section1 == {"LATAM agents": 1, "Other agents": 1, "Our offices": 2}


def test_the_no_mode_column_only_in_a_month_with_such_a_case(world):
    conn, repo, got, path = world
    march = cheat_page(path, MONTH)
    assert '<th class="num">Other</th><th class="num">No mode</th>' in march
    rows = {grp["name"]: grp["row"] for grp in cheatsheet.build(conn, MONTH).groups}
    assert (rows["LATAM agents"]["no_mode"], rows["Other agents"]["no_mode"],
            rows["Our offices"]["no_mode"]) == (1, 1, 0)
    april = cheat_page(path, CLEAN)
    assert "No mode" not in april and "no mode" not in april
    assert "Other 1 (Warehousing 1)" in april


def test_the_mode_text_names_truck_and_warehousing(world):
    conn, repo, got, path = world
    sheet = cheatsheet.build(conn, MONTH)
    tops = {q["who"]: q["mode"] for grp in sheet.groups for q in grp["top"]}
    assert tops["Wrenfield Cargo"] == "Air 1 · FCL 1 · LCL 2 · Truck 1 · no mode 1"
    assert tops["Pikestone Forwarding"] == "Customs only 1 · no mode 1"
    assert tops["JoJo JJV"] == "Warehousing 1 · Truck 1"
    lanes = {l["lane"]: l["mode"] for l in sheet.lanes}
    assert lanes["USA → Chile"] == "Air 1 · FCL 1 · LCL 2 · no mode 1"
    assert lanes["USA → China"] == "Customs only 1 · no mode 1"
    assert "other" not in " ".join(tops.values()) + " ".join(lanes.values())
