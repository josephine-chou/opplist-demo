"""Batch 17i: the Our offices in the month report - a bullet of their
own in section 14 and its table's office rows, a group of their own in
section 9, and page 3's tightening for one extra group and for two.

Every company, place, date and number here is invented; cases from
OPP-6801. The report month is April 2027 against March.
"""

from __future__ import annotations

import datetime as dt

import openpyxl
import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, monthly, monthly_analysis, monthly_export

MAR, APR, MAY = "2027-03", "2027-04", "2027-05"
TODAY = dt.date(2027, 5, 20)


@pytest.fixture
def world(tmp_path):
    conn = db.open_db(tmp_path / "offices.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    parties, n = {}, [6801]

    def party(key, ptype="Office", country="USA"):
        if key not in parties:
            parties[key] = add_party(repo, key, ptype=ptype, country=country)
        return parties[key]

    def cases(key, month, count, dest="China", ptype="Office", country="USA"):
        for i in range(count):
            case_id = add_enquiry(repo, "OPP-{}".format(n[0]), party(key, ptype, country),
                                  received="{}-{:02d}".format(month, i + 2), status="Quoted", service_mode="Air",
                                  origin_country="USA", dest_country=dest)
            if ptype in ("Office", "Direct Customer"):
                # SC1: from October the report counts an office's or a direct
                # customer's case only when it is LATAM; set by hand here, so
                # the lanes (and section 14's 'on LATAM lanes') stay as written
                repo.update("enquiry", case_id, {"latam_choice": "Y"})
            n[0] += 1
        repo.commit()

    return conn, cases


def growth(conn, month=APR):
    return monthly.build(conn, month, TODAY).analysis[2]


def bullets(card):
    return [monthly_analysis.plain(b) for b in card.bullets]


def agents(cases, latam=(1, 6), other=(2, 1)):
    """A LATAM agent and a non-LATAM agent, March and April counts: the LATAM
    agents lead section 14 unless a test says otherwise."""
    cases("Quarrystone Cargo", MAR, latam[0], "Chile", "Agent", "Chile")
    cases("Quarrystone Cargo", APR, latam[1], "Chile", "Agent", "Chile")
    cases("Wexcombe Freight", MAR, other[0], "China", "Agent", "China")
    cases("Wexcombe Freight", APR, other[1], "China", "Agent", "China")


# ------------------------------------------------------------ section 14 --
def test_the_office_bullet_names_three_risers_and_three_fallers_of_two_or_more(world):
    conn, cases = world
    agents(cases)
    for key, then, now in (("JoJo Ashby", 0, 4), ("JoJo Birch", 1, 4), ("JoJo Corran", 0, 2),
                           ("JoJo Gale", 0, 2), ("Harbour Desk", 0, 2), ("JoJo Dorle", 5, 0),
                           ("JoJo Elver", 4, 1), ("JoJo Fenn", 3, 1), ("JoJo Ivel", 1, 0)):
        cases(key, MAR, then)
        cases(key, APR, now, dest="Chile" if key == "JoJo Ashby" else "China")
    card = growth(conn)
    got = bullets(card)
    assert got[0].startswith("LATAM agents 1 → 6")
    # the other groups' bullet no longer names the offices; theirs is the last one
    assert got[-2] == "Other agents -1"
    assert got[-1] == ("Our offices 14 → 16: Ashby +4, Birch +3, Corran +2; Dorle -5, Elver -3, Fenn -2; "
                       "4 on LATAM lanes")
    # the numbers are bold, the names are not
    assert [t for t, bold in card.bullets[-1] if bold] == ["14", "16", "+4", "+3", "+2", "-5", "-3", "-2", "4"]


def test_with_no_change_of_two_the_changes_of_one_are_named(world):
    conn, cases = world
    agents(cases)
    for key, then, now in (("JoJo Ashby", 1, 2), ("Harbour Desk", 0, 1), ("JoJo Birch", 2, 1)):
        cases(key, MAR, then)
        cases(key, APR, now)
    # a key without 'JoJo ' in front as it is; nothing on a LATAM lane: no part
    assert bullets(growth(conn))[-1] == "Our offices 3 → 4: Ashby +1, Harbour Desk +1; Birch -1"


def test_an_office_bullet_with_nothing_after_a_to_b(world):
    conn, cases = world
    agents(cases)
    cases("JoJo Ashby", MAR, 1)
    cases("JoJo Ashby", APR, 4, dest="Chile")
    cases("JoJo Birch", MAR, 3)
    # Ashby +3, Birch -3, on LATAM lanes 4
    assert bullets(growth(conn))[-1] == "Our offices 4 → 4: Ashby +3; Birch -3; 4 on LATAM lanes"
    # May: nobody moved, nothing on a LATAM lane - a -> b alone, without the ':'
    cases("JoJo Ashby", MAY, 4)
    cases("JoJo Birch", "2027-05", 0)
    cases("Quarrystone Cargo", MAY, 1, "Chile", "Agent", "Chile")
    assert bullets(growth(conn, MAY))[-1] == "Our offices 4 → 4"


def test_when_the_offices_lead_their_bullet_leaves_out_a_to_b(world):
    conn, cases = world
    agents(cases, latam=(2, 3), other=(1, 1))
    cases("JoJo Ashby", APR, 5)
    cases("JoJo Birch", APR, 2, dest="Chile")
    got = bullets(growth(conn))
    assert got[0] == "Our offices 0 → 7, 88% of the +8"
    assert got[-2] == "LATAM agents +1, Other agents 0"
    assert got[-1] == "Our offices: Ashby +5, Birch +2; 2 on LATAM lanes"


def test_when_the_offices_lead_and_nothing_follows_there_is_no_office_bullet(world):
    conn, cases = world
    cases("JoJo Ashby", MAR, 2)
    cases("JoJo Ashby", APR, 3)
    # the offices are the only group: one bullet (the lead), no country moves, no others
    assert bullets(growth(conn)) == ["Our offices 2 → 3, 100% of the +1",
                                     "Our offices: Ashby +1"]
    cases("JoJo Birch", MAR, 1)
    cases("JoJo Birch", APR, 1)
    cases("JoJo Ashby", APR, 0)
    # Ashby +1 and Birch 0: still named; take Ashby's change away and nothing follows
    conn.execute("UPDATE enquiry SET scope = 'Out' WHERE received_on = '2027-04-04'")
    conn.commit()
    assert bullets(growth(conn)) == ["Our offices 3 → 3"]


def test_the_other_groups_bullet_is_left_out_when_only_the_offices_were_beside_the_lead(world):
    conn, cases = world
    cases("Quarrystone Cargo", MAR, 1, "Chile", "Agent", "Chile")
    cases("Quarrystone Cargo", APR, 6, "Chile", "Agent", "Chile")
    cases("JoJo Ashby", MAR, 1)
    cases("JoJo Ashby", APR, 3)
    got = bullets(growth(conn))
    assert got[0].startswith("LATAM agents 1 → 6") and not any(b.startswith("Other agents") for b in got)
    assert not any("Our offices +" in b for b in got)
    assert got[-1] == "Our offices 1 → 3: Ashby +2"


def test_section_14s_table_lists_every_office_after_the_groups(world, tmp_path):
    conn, cases = world
    agents(cases)
    for key, then, now in (("JoJo Birch", 1, 3), ("JoJo Ashby", 2, 3), ("JoJo Corran", 4, 0),
                           ("JoJo Dorle", 0, 1)):
        cases(key, MAR, then)
        cases(key, APR, now)
    report = monthly.build(conn, APR, TODAY)
    table = report.analysis[2].table
    at = [(r.label, r.cls) for r in table.rows].index(("Our offices", "head"))
    # most this month first, then most last month, then by name
    assert [(r.label, r.cells) for r in table.rows[at + 1:]] == [
        ("JoJo Ashby", ["2", "3", "+1"]), ("JoJo Birch", ["1", "3", "+2"]),
        ("JoJo Dorle", ["0", "1", "+1"]), ("JoJo Corran", ["4", "0", "-4"])]
    wb = openpyxl.load_workbook(monthly_export.write_xlsx(report, tmp_path))
    try:
        got = [list(r) for r in wb["Report"].iter_rows(values_only=True)]
    finally:
        wb.close()
    first = [r[0] for r in got]
    at = first.index("14. Where the growth came from")
    head = first.index("Our offices", at + 5)        # the group row comes first, the head after it
    assert [r[:4] for r in got[head + 1:head + 5]] == [
        ["JoJo Ashby", 2, 3, 1], ["JoJo Birch", 1, 3, 2], ["JoJo Dorle", 0, 1, 1],
        ["JoJo Corran", 4, 0, -4]]
    assert got[head + 5][0].startswith("LATAM agents 1 → 6")            # the bullets follow as notes
    assert "Our offices 7 → 7: Birch +2; Corran -4" in first
    text = monthly.render_text(report)
    assert "  JoJo Corran | 4 | 0 | -4" in text
    # the page shows the bullets, not the table
    assert "JoJo Corran" not in monthly_export.render_html(report).split("14</span>Where the growth")[1]


# ------------------------------------------------------------- section 9 --
def section_9(conn, month=APR):
    return monthly.build(conn, month, TODAY).top_agents.rows


def test_section_9_has_the_three_offices_with_the_most_inquiries(world):
    conn, cases = world
    agents(cases)
    for key, now in (("JoJo Ashby", 1), ("JoJo Birch", 3), ("JoJo Corran", 1), ("JoJo Dorle", 2)):
        cases(key, APR, now)
    cases("Kestle Pottery", APR, 2, "China", "Direct Customer")
    rows = section_9(conn)
    heads = [r.label for r in rows if r.cls == "head"]
    assert heads == ["LATAM agents", "Other agents", "Our offices", "Direct customers"]
    at = [r.label for r in rows].index("Our offices")
    # the most first, a tie by the cases' order as in the other groups
    assert [(r.label, r.cells[0]) for r in rows[at + 1:at + 4]] == [
        ("JoJo Birch", "3"), ("JoJo Dorle", "2"), ("JoJo Ashby", "1")]
    assert rows[at + 4].label == "Direct customers"
    assert len(rows[at + 1].cells) == 9                       # the same columns, March's among them
    report = monthly.build(conn, APR, TODAY)
    assert report.office_group and report.direct_group
    # the xlsx and the text follow the table
    assert any(line.startswith("  JoJo Birch | 3 |") for line in monthly.render_text(report))


def test_section_9_has_no_office_group_in_a_month_without_an_office_request(world):
    conn, cases = world
    agents(cases)
    cases("JoJo Ashby", MAR, 2)
    heads = [r.label for r in section_9(conn) if r.cls == "head"]
    assert heads == ["LATAM agents", "Other agents"]
    assert not monthly.build(conn, APR, TODAY).office_group


# ---------------------------------------------------------------- page 3 --
def page_3(report):
    html = monthly_export.render_html(report)
    start = html.index("<small>Agents</small>")
    return html[html.rindex("<section", 0, start):html.index("</section>", start)]


def test_page_3_takes_dc_with_one_extra_group_and_dc4_with_both(world):
    conn, cases = world
    agents(cases)
    plain = monthly.build(conn, APR, TODAY)
    assert plain.page3_class == "" and '<section class="page">' in page_3(plain)
    assert 'viewBox="0 0 640 200"' in page_3(plain)
    cases("JoJo Ashby", APR, 1)
    one = monthly.build(conn, APR, TODAY)
    assert one.page3_class == " dc"
    assert '<section class="page dc">' in page_3(one) and 'viewBox="0 0 640 180"' in page_3(one)
    assert 'width="150" height="150"' in page_3(one)
    cases("Kestle Pottery", APR, 1, "China", "Direct Customer")
    both = monthly.build(conn, APR, TODAY)
    assert both.page3_class == " dc dc4"
    body = page_3(both)
    assert '<section class="page dc dc4">' in body and 'viewBox="0 0 640 130"' in body
    assert 'width="120" height="120"' in body                                   # the donut, 20 thick
    css = monthly_export.css()
    for rule in (".page.dc4 h1{margin-bottom:12px}", ".page.dc4 h2{margin-bottom:6px}",
                 ".page.dc4 .p3low{margin-top:12px}",
                 ".page.dc4 table.ta td,.page.dc4 table.ta th{padding:1px 8px}",
                 ".page.dc4 table.ta tr.head td{padding-top:1px}",
                 ".page.dc4 table.ta td,.page.dc4 table.ta th{line-height:1.2}",
                 ".page.dc4 .p3low .chartbox svg{max-height:none}"):
        assert rule in css, rule
    # the second step comes after dc and the dense rules, so it wins
    assert css.index(".page.dc4 h1") > css.index(".page.dense .p3low{") > css.index(".page.dc .p3low")
