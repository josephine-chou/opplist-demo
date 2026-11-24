"""Batch 21b: a direct customer's case received before 1 Oct 2026 is
not counted. SC1: it is registered In like any
other; the report rule leaves it out. Every company, place and date here is invented; cases
from OPP-5601.
"""

from __future__ import annotations

import pytest

from conftest import add_party, seed_reference
from opplist import db, direct_customers_out, monthly
from opplist.web import create_app


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "cut.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "next_opp_number", "5601")
    ids = {"bramble": add_party(repo, "Brambleton Tiles", ptype="Direct Customer", country="USA"),
           "wren": add_party(repo, "Wrenfield Cargo", country="Chile")}       # an agent
    repo.commit()
    yield conn, repo, ids, path
    conn.close()


def client(path):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client()


def register(c, party, received, token):
    return c.post("/enquiries/new", follow_redirects=True, data={
        "received_on": received, "requester_party_id": str(party), "service_mode": "Air",
        "gross_kg": "300", "gross_unit": "kg",
        "origin_country": "USA", "dest_country": "Chile", "decision": "new", "form_token": token})


def scope(conn, opp):
    return conn.execute("SELECT scope FROM enquiry WHERE opp_no = ?", (opp,)).fetchone()[0]


def test_registering_on_the_page_keeps_every_case_in(world):
    # SC1: registration never sets scope; September's direct customer is In
    conn, repo, ids, path = world
    c = client(path)
    for party, received, token in ((ids["bramble"], "2026-09-30", "t1"), (ids["bramble"], "2026-10-01", "t2"),
                                   (ids["wren"], "2026-09-30", "t3")):
        page = register(c, party, received, token).data.decode("utf-8")
        assert "scope Out" not in page
    assert [scope(conn, o) for o in ("OPP-5601", "OPP-5602", "OPP-5603")] == ["In", "In", "In"]


def test_the_repository_rule_blank_date_and_a_given_scope(world):
    conn, repo, ids, path = world
    blank = repo.create_enquiry({"requester_party_id": ids["bramble"], "service_mode": "Air"}, "2026-10-07")
    given = repo.create_enquiry({"requester_party_id": ids["bramble"], "received_on": "2026-09-12",
                                 "scope": "In"}, "2026-09-12")
    repo.commit()
    rows = dict(conn.execute("SELECT id, scope FROM enquiry").fetchall())
    assert (rows[blank], rows[given]) == ("In", "In")        # SC1: In, the rule is the report's
    assert direct_customers_out.reason_of(conn, blank) == "before October"
    # SC1: out_reason(type, received, origin, dest, the case's latam flag)
    rule = direct_customers_out.out_reason
    assert rule("Direct Customer", "2026-09-30 17:40", "USA", "Chile", "Y") == "before October"
    assert not rule("Direct Customer", "2026-10-01", "USA", "Chile", "Y")
    assert not rule("Agent", None, "USA", "China", "N")


def test_a_split_keeps_its_source_scope(world):
    conn, repo, ids, path = world
    case = repo.create_enquiry({"requester_party_id": ids["bramble"], "received_on": "2026-09-20",
                                "service_mode": "Air", "scope": "In"}, "2026-09-20")
    line = repo.add_cargo_line(case, {"pieces": 3, "pack_word": "crates", "weight_value": 410, "weight_unit": "kg"})
    repo.add_cargo_line(case, {"pieces": 1, "pack_word": "crate", "weight_value": 95, "weight_unit": "kg"})
    new = repo.split_cargo_line(line, "2026-10-07")
    repo.commit()
    assert conn.execute("SELECT scope FROM enquiry WHERE id = ?", (new,)).fetchone()[0] == "In"


def test_switched_to_in_under_more_stays_in_after_a_save(world):
    conn, repo, ids, path = world
    c = client(path)
    register(c, ids["bramble"], "2026-09-30", "t1")
    case = conn.execute("SELECT id FROM enquiry WHERE opp_no = 'OPP-5601'").fetchone()[0]
    c.post("/enquiries/{}/scope".format(case), data={"scope": "In"})
    repo.save_enquiry(case, {"commodity": "Glazed floor tiles", "received_on": "2026-09-29"})
    repo.commit()
    assert scope(conn, "OPP-5601") == "In"


def test_the_report_leaves_september_direct_customers_out_and_counts_october(world):
    # SC1: no check-box group; the base leaves a September direct customer
    # out whatever its scope, and counts October's LATAM one
    conn, repo, ids, path = world
    sep = repo.create_enquiry({"requester_party_id": ids["bramble"], "received_on": "2026-09-18",
                               "service_mode": "Air", "origin_country": "USA", "dest_country": "Chile",
                               "scope": "In"}, "2026-09-18")
    oct_ = repo.create_enquiry({"requester_party_id": ids["bramble"], "received_on": "2026-10-02",
                                "service_mode": "Air", "origin_country": "USA", "dest_country": "Chile"}, "2026-10-02")
    repo.commit()
    assert "Direct customer before October, counted" not in [g["title"] for g in monthly.checks(conn, "2026-09")]

    def counted(month):
        return [r[0] for r in conn.execute("SELECT e.id {} WHERE e.month = ? AND {}".format(
            monthly.BASE, monthly.CASES), (month,))]
    assert counted("2026-09") == [] and sep
    assert counted("2026-10") == [oct_]
