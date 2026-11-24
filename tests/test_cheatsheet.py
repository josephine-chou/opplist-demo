"""Batch 17c: the cheat sheet, the Report page's second tab - same month,
same base and groups as the month report (sample v2).

Every company, place, date, number and cargo here is invented; cases from
OPP-5301.
"""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import cheatsheet, db
from opplist.web import create_app

MONTH = "2026-09"


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "cs.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    for name in ("Peru", "Colombia", "Mexico", "Ecuador", "Bolivia"):
        repo.insert("country", {"name": name, "region": "Latin America"})
    for key, country in (("SCL", "Chile"), ("LIM", "Peru"), ("BOG", "Colombia"), ("DTW", "USA")):
        repo.insert("location", {"key": key, "country": country, "type": "City"})
    p = {"quill": add_party(repo, "Quillmoor Cargo", country="Chile"),
         "fern": add_party(repo, "Fernhollow Freight", country="Peru"),
         "lark": add_party(repo, "Larkspur Logistics", country="USA"),
         "office": add_party(repo, "JoJo JJV", ptype="Office", country="Unknown"),
         "odd": add_party(repo, "Odd Requester", ptype="Unknown", country="Unknown")}
    for i in range(6):                                   # six more LATAM agents, one case each
        p["small{}".format(i)] = add_party(repo, "Small Andes Agent {}".format(i), country="Chile")
    n = [5301]

    def case(party, status="Quoted", mode="Air", origin="USA", dest="Chile", o_loc=None, d_loc=None, **extra):
        values = dict(received="2026-09-10", status=status, service_mode=mode, origin_country=origin,
                      dest_country=dest, origin_loc=o_loc, dest_loc=d_loc)
        values.update(extra)
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        return add_enquiry(repo, opp, p[party], **{k: v for k, v in values.items() if v is not None})

    # Quillmoor: five lanes - three shown, '+2 more lanes'
    case("quill", "Won", "Air", "USA", "Chile", "MIA", "SCL", commodity="Industrial heat exchanger plates, stainless")
    case("quill", "Quoted", "Air", "USA", "Chile", "MIA", "SCL")
    case("quill", "Quoted", "FCL", "USA", "Chile", "DTW", "SCL")
    case("quill", "Quoted", "Air", "USA", "Chile", "LAX", "SCL")
    case("quill", "Quoted", "LCL", "USA", "Peru", "MIA", "LIM")
    case("quill", "Quoted", "LCL", "USA", "Peru")
    case("quill", "Lost", "RORO", "USA", "Colombia", closed_reason="Price too high")
    case("quill", "Quoted", "Air", "USA", "Ecuador")
    case("quill", "Quoted", "Air", "USA", "Bolivia")
    case("fern", "Won", "FCL", "China", "Peru", "SHANGHAI", "LIM")
    case("lark", "Quoted", "Air", "USA", None)                         # one country: 'from USA'
    case("lark", "Won", "Air", "USA", "China")
    case("office", "Quoted", "FCL", "Mexico", "USA")
    case("odd", "Quoted", "Air", "USA", "Chile")                       # not on the sheet: an Unknown requester
    for i in range(6):
        case("small{}".format(i), "Quoted", "Air", "USA", "Chile")
    repo.commit()
    yield conn, repo, p, path
    conn.close()


def page(path, **args):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client().get("/report", query_string=dict({"month": MONTH}, **args)).data.decode("utf-8")


def test_the_tab_renders_keeps_its_month_and_has_no_export_buttons(book):
    conn, repo, p, path = book
    html = page(path, view="cheat")
    assert "CHEAT SHEET <small>September 2026</small>" in html
    assert '<a href="/report?month=2026-09&amp;view=cheat" class="on">Cheat sheet</a>' in html
    assert 'href="/report?month=2026-08&amp;view=cheat"' in html and 'href="/report?month=2026-10&amp;view=cheat"' in html
    assert '<input type="hidden" name="view" value="cheat">' in html
    assert "Export report HTML" not in html and "/export/report" not in html
    report = page(path)
    assert "Export report HTML" in report and '<a href="/report?month=2026-09" class="on">Report</a>' in report


def test_the_glance_line_adds_up(book):
    conn, repo, p, path = book
    g = cheatsheet.build(conn, MONTH).glance
    assert g["n"] == 19                                                 # the Unknown requester left out
    assert g["latam"] + g["row"] == g["n"]
    assert g["air"] + g["fcl"] + g["lcl"] + g["other"] == g["n"] and g["other"] == 1
    assert (g["won"], g["lost"], g["cancelled"]) == (3, 1, 0)
    html = page(path, view="cheat")
    assert "<span><b>19</b> inquiries</span>" in html and "&middot; Other 1 (RORO 1)" in html and "no mode" not in html and "Cancelled" not in html


def test_the_by_who_asked_rows_and_most_inquiries(book):
    conn, repo, p, path = book
    sheet = cheatsheet.build(conn, MONTH)
    rows = {g["name"]: g["row"] for g in sheet.groups}
    # SC1: 'Non-LATAM agents' is 'Other agents'
    assert list(rows) == ["LATAM agents", "Other agents", "Our offices"]       # no Direct customers
    latam = rows["LATAM agents"]
    assert (latam["n"], latam["share"], latam["air"], latam["fcl"], latam["lcl"], latam["other"]) == (16, 84, 11, 2, 2, 1)
    assert latam["most"] == "Quillmoor Cargo (9)" and latam["won"] == 2
    assert rows["Other agents"]["most"] == "Larkspur Logistics (2)"
    assert (rows["Our offices"]["import"], rows["Our offices"]["export"]) == (1, 0)


def test_a_requester_s_lanes_pairs_more_lanes_and_the_also_line(book):
    conn, repo, p, path = book
    group = cheatsheet.build(conn, MONTH).groups[0]
    quill = group["top"][0]
    assert (quill["who"], quill["country"], quill["n"], quill["mode"], quill["won"]) == \
        ("Quillmoor Cargo", "Chile", 9, "Air 5 · FCL 1 · LCL 2 · RORO 1", 1)
    assert quill["lines"][0] == {"lane": "USA → Chile", "n": 4,
                                 "pairs": "MIA → SCL ×2, DTW → SCL"}
    assert quill["lines"][1] == {"lane": "USA → Peru", "n": 2, "pairs": "MIA → LIM"}
    assert quill["more"] == 2
    assert group["requesters"] == 8 and len(group["top"]) == 5
    assert group["also"] == "Small Andes Agent 3 1, Small Andes Agent 4 1, Small Andes Agent 5 1"
    html = page(path, view="cheat")
    assert '<div class="ln more">+2 more lanes</div>' in html
    assert "Also: Small Andes Agent 3 1" in html
    office = cheatsheet.build(conn, MONTH).groups[2]["top"][0]
    assert office["country"] == ""                                       # Unknown reads blank


def test_one_country_is_in_no_lane_table_and_top_lanes_name_who_asked(book):
    conn, repo, p, path = book
    sheet = cheatsheet.build(conn, MONTH)
    lark = [g for g in sheet.groups if g["name"] == "Other agents"][0]["top"][0]          # SC1: the new name
    assert [l["lane"] for l in lark["lines"]] == ["from USA", "USA → China"]
    lanes = {l["lane"]: l for l in sheet.lanes}
    assert not any(l.startswith(("from ", "to ")) for l in lanes)
    chile = lanes["USA → Chile"]
    assert chile["n"] == 10 and chile["who"] == "Quillmoor Cargo 4, Small Andes Agent 0 1, Small Andes Agent 1 1 +4 more"
    assert len(sheet.lanes) <= cheatsheet.TOP_LANES


def test_the_won_list_cuts_the_cargo(book):
    conn, repo, p, path = book
    won = cheatsheet.build(conn, MONTH).won
    assert [w["opp_no"] for w in won] == ["OPP-5301", "OPP-5310", "OPP-5312"]
    first = won[0]
    assert (first["where"], first["pair"], first["mode"]) == ("USA → Chile", "MIA → SCL", "Air")
    assert first["cargo"] == "Industrial heat exchanger plates, stainl" and len(first["cargo"]) == 40
    assert won[2]["pair"] == ""
    html = page(path, view="cheat")
    assert "Won in September <small>3 inquiries</small>" in html


def test_no_other_not_set_or_question_mark_on_the_page(book):
    conn, repo, p, path = book
    html = page(path, view="cheat")
    body = html[html.index('<section class="page cs">'):]
    # the By who asked table's mode column is called Other (the brief's
    # column); no group, requester or place ever is
    # (CS1: and the glance's Other, the report's Other with its parts;
    # SC1: and the group Other agents, its name since SC1)
    assert body.count('<th class="num">Other</th>') == 1
    text = re.sub(r"<[^>]+>", " ", body.replace('<th class="num">Other</th>', "").replace("&middot; Other 1 (", "")
                  .replace("Other agents", ""))
    for word in ("Other", "Not set", "Not recorded", "?"):
        assert word not in text, word
    assert "Odd Requester" not in body
