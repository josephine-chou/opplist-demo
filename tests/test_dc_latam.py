"""Batch DC1 (schema v47): a direct customer's case counts only when it
touches LATAM - a country of the case, or the customer's own, in the LATAM
list of Settings. SC1: the rule is the report's (direct_customers_out
.out_reason reads the case's latam flag), scope is never set by it, and
not-latam-out only lists.

Every company, place, date and number here is invented; cases from
OPP-8501.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, direct_customers_out as dco, monthly
from opplist.__main__ import main
from opplist.web import create_app

LATAM = {"Chile", "Brazil", "Mexico"}
NOV = "2026-11"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "dc1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    repo.insert("country", {"name": "Canada", "region": "North America"})
    db.set_setting(conn, "next_opp_number", "8501")
    p = {"mill": add_party(repo, "Halvering Mills", ptype="Direct Customer", country="USA"),
         "cafe": add_party(repo, "Pradoverde Roasters", ptype="Direct Customer", country="Brazil"),
         "agent": add_party(repo, "Kestrelby Freight", country="USA")}
    repo.commit()
    n = [8551]                 # create_enquiry numbers from 8501

    def case(party, received="2026-11-04", origin="USA", dest="China", scope="In", **extra):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        got = add_enquiry(repo, opp, p[party], received=received, status="Quoted", service_mode="Air",
                          origin_country=origin, dest_country=dest, scope=scope, **extra)
        repo.commit()
        return got
    yield conn, repo, p, case, path
    conn.close()


def scope(conn, case_id):
    return conn.execute("SELECT scope FROM enquiry WHERE id = ?", (case_id,)).fetchone()[0]


def client(path):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client()


# ------------------------------------------------------------- the rule --
# SC1: out_reason(type, received, origin, dest, latam flag, her choice)
@pytest.mark.parametrize("ptype, received, origin, dest, latam, reason", [
    ("Direct Customer", "2026-09-30", "USA", "Chile", "Y", "before October"),
    ("Direct Customer", None, "USA", "Chile", "Y", "before October"),
    ("Direct Customer", "2026-10-01", "USA", "China", "N", "not LATAM"),
    ("Direct Customer", "2026-11-04 09:12", "China", "USA", "N", "not LATAM"),
    ("Direct Customer", "2026-10-01", "Chile", "USA", "Y", ""),            # origin LATAM
    ("Direct Customer", "2026-10-01", "China", "Mexico", "Y", ""),         # destination LATAM
    ("Direct Customer", "2026-10-01", "USA", "China", "Y", ""),            # the customer LATAM (the flag)
    ("Direct Customer", "2026-10-01", "USA", None, "N", ""),               # a country missing
    ("Direct Customer", "2026-10-01", "Unknown", "China", "N", ""),        # Unknown is missing
    ("Agent", "2026-10-01", "USA", "China", "N", ""),
])
def test_each_branch_of_the_rule(ptype, received, origin, dest, latam, reason):
    assert dco.out_reason(ptype, received, origin, dest, latam) == reason


def test_a_customer_in_a_latam_country_is_in_and_latam_in_v_enquiry_and_the_report(world):
    conn, repo, p, case, path = world
    got = repo.create_enquiry({"requester_party_id": p["cafe"], "received_on": "2026-11-04", "service_mode": "Air",
                               "origin_country": "USA", "dest_country": "China"}, "2026-11-04")
    repo.commit()
    assert scope(conn, got) == "In"
    row = conn.execute("SELECT latam, latam_lane, latam_agent FROM v_enquiry WHERE id = ?", (got,)).fetchone()
    assert tuple(row) == ("Y", "N", "N")
    route = conn.execute("SELECT latam FROM v_route WHERE enquiry_id = ?", (got,)).fetchone()[0]
    assert route == "Y"
    rows = [(r.label, r.cells[0] if r.cells else "") for r in monthly.build(conn, NOV).by_scope.rows]
    latam_at = rows.index(("LATAM", "1"))
    assert rows[latam_at + 1] == ("Direct customers", "1")


# ------------------------------------------------------ registration --
def register(c, party, dest, token, origin="USA"):
    return c.post("/enquiries/new", follow_redirects=True, data={
        "received_on": "2026-11-04", "requester_party_id": str(party), "service_mode": "Air",
        "gross_kg": "300", "gross_unit": "kg", "origin_country": origin, "dest_country": dest,
        "decision": "new", "form_token": token}).data.decode("utf-8")


def test_registration_keeps_every_case_in_and_the_report_leaves_the_not_latam_one_out(world):
    # SC1: registration never sets scope; the report rule leaves OPP-8501 out
    conn, repo, p, case, path = world
    c = client(path)
    page = register(c, p["mill"], "China", "t1")
    assert "scope Out" not in page
    register(c, p["mill"], "Chile", "t2")
    register(c, p["agent"], "China", "t3")
    got = dict(conn.execute("SELECT opp_no, scope FROM enquiry").fetchall())
    assert got == {"OPP-8501": "In", "OPP-8502": "In", "OPP-8503": "In"}
    total = [r for r in monthly.build(conn, NOV).by_scope.rows if r.label == "Total"][0]
    assert total.cells[0] == "2"


def test_an_edit_never_changes_the_scope(world):
    conn, repo, p, case, path = world
    got = repo.create_enquiry({"requester_party_id": p["mill"], "received_on": "2026-11-04", "service_mode": "Air",
                               "origin_country": "USA"}, "2026-11-04")
    repo.commit()
    assert scope(conn, got) == "In"                       # a country missing: not decided
    repo.save_enquiry(got, {"dest_country": "China"})
    repo.update("party", p["mill"], {"country": "USA"})
    repo.commit()
    assert scope(conn, got) == "In"
    agent_case = case("agent")
    repo.update("enquiry", agent_case, {"requester_party_id": p["mill"]})
    repo.commit()
    assert scope(conn, agent_case) == "In"
    # SC1: no check-box group any more - the report leaves both out by itself
    assert not [g for g in monthly.checks(conn, NOV) if "not LATAM" in g["title"]]
    assert dco.reason_of(conn, got) == dco.reason_of(conn, agent_case) == dco.NOT_LATAM


# ------------------------------------------------------------ the run --
@pytest.fixture
def mixed(world):
    conn, repo, p, case, path = world
    got = {"china": case("mill"), "chile": case("mill", dest="Chile"), "cafe": case("cafe"),
           "no_dest": case("mill", dest=None), "sept": case("mill", received="2026-09-12", dest="Chile"),
           "out": case("mill", scope="Out"), "agent": case("agent"), "hers": case("mill", origin="China",
                                                                               dest="USA"),
           "earlier": case("mill", dest="Canada")}
    yield conn, repo, got, path


def test_the_run_lists_read_only(mixed):
    # SC1: the cases the report leaves out, whatever their scope; nothing written
    conn, repo, got, path = mixed
    before = conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    report = dco.run(conn)
    assert [(r[0], r[4], r[5], r[7]) for r in report.rows] == [
        ("OPP-8555", "before October", "USA to Chile", "In"), ("OPP-8551", "not LATAM", "USA to China", "In"),
        ("OPP-8556", "not LATAM", "USA to China", "Out"), ("OPP-8558", "not LATAM", "China to USA", "In"),
        ("OPP-8559", "not LATAM", "USA to Canada", "In")]
    assert conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == before
    assert {scope(conn, i) for k, i in got.items() if k != "out"} == {"In"}
    text = "\n".join(report.lines())
    assert text.startswith("read only")
    assert "  OPP-8551  2026-11-04  Halvering Mills  USA to China  Quoted" in text
    assert "  OPP-8556  2026-11-04  Halvering Mills  USA to China  Quoted  (scope Out)" in text


def test_apply_is_refused_and_writes_nothing(mixed, tmp_path, capsys):
    # SC1: --apply no longer sets Out
    conn, repo, got, path = mixed
    conn.close()
    restore = tmp_path / "restore"
    assert main(["direct-customers-out", "--db", str(path), "--apply", "--restore-dir", str(restore)]) == 2
    assert dco.NO_APPLY in capsys.readouterr().out
    assert not restore.exists() or not list(restore.glob("*.db"))
    conn = db.open_db(path)
    try:
        assert {k: scope(conn, i) for k, i in got.items()}["china"] == "In"
    finally:
        conn.close()
