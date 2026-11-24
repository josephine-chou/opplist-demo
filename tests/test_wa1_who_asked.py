"""Batch WA1 (2026-11-06): Who asked? on the team site for a case parked on
(TBC) - lists/parties, the 'tbc' flag and the 'requester' action; another
own office's case not counted when it does not touch LATAM, as a
direct customer's is; the case number leads the case page's h1. SC1: the
rule is the report's - registration and the requester leaving (TBC) never
set scope, and not-latam-out only lists.

Every company, person, place and date here is invented; cases from
OPP-9101.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, direct_customers_out as dco, firebase_pull as fp, firebase_sync as fs
from opplist.__main__ import main
from opplist.web import create_app
from test_firebase_pull import FakeSite

TODAY = "2026-11-06"


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "wa1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "latam_countries", ["Chile", "Brazil", "Mexico"])
    db.set_setting(conn, "owners", ["Ana", "Ben"])
    db.set_setting(conn, "next_opp_number", "9151")
    p = {"tbc": add_party(repo, "(TBC)", placeholder=1),
         "unknown": add_party(repo, "Unknown", placeholder=1),
         "office": add_party(repo, "JoJo Varnholm", ptype="Office", country="China"),
         "agent": add_party(repo, "Lindqvist Forwarding", country="Chile"),
         "direct": add_party(repo, "Ormsby Tooling", ptype="Direct Customer", country="USA"),
         "carrier": add_party(repo, "Skyreach Air", ptype="Carrier")}
    p["merged"] = add_party(repo, "Lindqvist Fwd Old", merged_into=p["agent"])
    repo.commit()
    n = [9101]

    def case(party, received="2026-11-04", origin="USA", dest="China", scope="In", **extra):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        got = add_enquiry(repo, opp, p[party], received=received, service_mode="Air", origin_country=origin,
                          dest_country=dest, scope=scope, source_subject="RATE REQUEST // {}".format(opp), **extra)
        repo.commit()
        return got
    yield conn, repo, p, case, path
    conn.close()


def scope(conn, case_id):
    return conn.execute("SELECT scope FROM enquiry WHERE id = ?", (case_id,)).fetchone()[0]


def notes(conn, case_id):
    return [r[0] for r in conn.execute("SELECT note FROM status_event WHERE enquiry_id = ? AND kind = 'note' "
                                       "AND voided = 0 ORDER BY id", (case_id,))]


def client(path):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client()


def act(repo, action_id, opp, **fields):
    action = dict(fields, type="requester", opp=opp, by="dee@example.test", name="Dee Rowan", owner="Ana",
                  app="test", at=dt.datetime(2026, 11, 6, 15, 0, tzinfo=dt.timezone.utc))
    site = db.Repository(repo.conn, source=fp.SOURCE)              # as pull_actions takes it in
    ok, message = fp.take_in(site, action_id, action, TODAY)
    site.commit()
    return ok, message


# ------------------------------------------------------- what is pushed --
def test_lists_parties_holds_the_three_types_and_leaves_out_merged_and_placeholders(world):
    conn, repo, p, case, path = world
    doc = fs.parties_doc(conn)
    assert doc == {"parties": [
        {"id": p["office"], "key": "JoJo Varnholm", "type": "Office", "country": "China"},
        {"id": p["agent"], "key": "Lindqvist Forwarding", "type": "Agent", "country": "Chile"},
        {"id": p["direct"], "key": "Ormsby Tooling", "type": "Direct Customer", "country": "USA"}]}


def test_the_push_writes_lists_parties_with_at_only_when_it_changes_and_flags_tbc(world, tmp_path):
    conn, repo, p, case, path = world
    parked, known = case("tbc"), case("agent")
    conn.close()
    fake = FakeSite()
    kw = dict(client=fake, state_file=tmp_path / "fs.json", reports_dir=tmp_path / "reports",
              record=tmp_path / "exports.json", today=TODAY)
    fs.sync(path, **kw)
    assert fake.docs["lists/parties"]["at"] == "REQUEST_TIME"
    assert len(fake.docs["lists/parties"]["parties"]) == 3
    rows = {r["opp"]: r for r in fake.docs["lists/inquiries"]["rows"]}
    assert rows["OPP-9101"]["tbc"] is True and "tbc" not in rows["OPP-9102"]
    assert fake.docs["cases/OPP-9101"]["tbc"] is True and "tbc" not in fake.docs["cases/OPP-9102"]
    fs.sync(path, **kw)
    assert not any("lists/parties" in c for c in fake.commits[2:])          # unchanged: not rewritten
    conn = db.open_db(path)
    add_party(db.Repository(conn, source="test"), "Aldersgate Freight", country="Brazil")
    conn.commit()
    conn.close()
    fs.sync(path, **kw)
    assert len(fake.docs["lists/parties"]["parties"]) == 4


# ------------------------------------------------------- the action --
def test_the_action_sets_the_requester_and_writes_the_note(world):
    conn, repo, p, case, path = world
    got = case("tbc", dest="Chile")
    ok, message = act(repo, "a1", "OPP-9101", partyId=p["agent"], text="")
    assert ok, message
    assert message == "OPP-9101: the requester is now Lindqvist Forwarding."
    row = conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?", (got,)).fetchone()
    assert row[0] == p["agent"] and scope(conn, got) == "In"
    assert notes(conn, got) == ["Requester from the team site, Dee Rowan: Lindqvist Forwarding"]
    logged = conn.execute("SELECT source FROM change_log WHERE table_name = 'enquiry' AND row_id = ? "
                          "AND field = 'requester_party_id'", (got,)).fetchone()
    assert logged[0] == "web"
    assert conn.execute("SELECT result FROM site_action WHERE id = 'a1'").fetchone()[0] == "applied"


def test_the_action_is_refused_on_a_case_that_is_not_tbc_and_for_a_merged_party(world):
    conn, repo, p, case, path = world
    case("agent")
    case("tbc")
    ok, message = act(repo, "a1", "OPP-9101", partyId=p["direct"])
    assert not ok and message == ("OPP-9101 already has its requester (Lindqvist Forwarding), "
                                  "so the requester was not set.")
    ok, message = act(repo, "a2", "OPP-9102", partyId=p["merged"])
    assert not ok and "was merged into another party" in message
    ok, message = act(repo, "a3", "OPP-9102", partyId=p["unknown"])
    assert not ok and "is a placeholder" in message
    ok, message = act(repo, "a4", "OPP-9102", partyId=99999)
    assert not ok and "party 99999 is not in OppList" in message
    ok, message = act(repo, "a5", "OPP-9102", partyId=0, text="  ")
    assert not ok and "no party and no text" in message
    assert conn.execute("SELECT COUNT(*) FROM site_action WHERE result = 'refused'").fetchone()[0] == 5
    assert dco.is_tbc(conn, conn.execute("SELECT requester_party_id FROM enquiry WHERE opp_no = 'OPP-9102'"
                                         ).fetchone()[0])


def test_a_text_alone_writes_only_the_note_and_leaves_tbc(world):
    conn, repo, p, case, path = world
    got = case("tbc")
    ok, message = act(repo, "a1", "OPP-9101", partyId=0, text="Victor's WeChat contact at Brightwater Pumps")
    assert ok and "Iris sets the requester" in message
    assert notes(conn, got) == ["Who asked, from the team site, Dee Rowan: Victor's WeChat contact at "
                                "Brightwater Pumps"]
    assert conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?", (got,)).fetchone()[0] == p["tbc"]


def test_a_tbc_case_that_gets_an_office_from_the_site_stays_in(world):
    # SC1: the site's requester action leaves scope alone; the report rule
    # leaves the not-LATAM case out by itself
    conn, repo, p, case, path = world
    china, chile = case("tbc"), case("tbc", dest="Chile")
    ok, message = act(repo, "a1", "OPP-9101", partyId=p["office"])
    assert ok and message == "OPP-9101: the requester is now JoJo Varnholm."
    assert scope(conn, china) == "In"
    assert notes(conn, china) == ["Requester from the team site, Dee Rowan: JoJo Varnholm"]
    assert dco.reason_of(conn, china) == dco.OWN_OFFICE
    ok, message = act(repo, "a2", "OPP-9102", partyId=p["office"])
    assert ok and scope(conn, chile) == "In" and dco.reason_of(conn, chile) == ""


def test_her_edit_on_the_case_page_from_tbc_leaves_scope_alone(world):
    # SC1: was 'runs the rule once'
    conn, repo, p, case, path = world
    parked = case("tbc")
    c = client(path)
    page = c.post("/enquiries/{}".format(parked), data={"requester_party_id": str(p["direct"])},
                  follow_redirects=True).data.decode("utf-8")
    assert "scope Out" not in page
    conn.rollback()
    assert scope(conn, parked) == "In" and notes(conn, parked) == []
    assert dco.reason_of(conn, parked) == dco.NOT_LATAM


# ------------------------------------------------------- the scope rule --
# SC1: out_reason reads the case's latam flag (v_enquiry.latam); an office's
# own country is not in that flag, so the row 'the office in a LATAM
# country' went
@pytest.mark.parametrize("received, origin, dest, latam, reason", [
    ("2026-10-01", "USA", "China", "N", "own office, not LATAM"),
    ("2026-11-04", "China", "Chile", "Y", ""),                       # LATAM destination
    ("2026-11-04", "USA", None, "N", ""),                            # a country missing
    ("2026-09-30", "USA", "China", "N", ""),                         # before October: not touched
    (None, "USA", "China", "N", ""),                                 # no date: not touched
])
def test_the_rule_for_an_office(received, origin, dest, latam, reason):
    assert dco.out_reason("Office", received, origin, dest, latam) == reason


def test_an_office_case_registers_in_whatever_its_countries(world):
    # SC1: was registers Out when not LATAM
    conn, repo, p, case, path = world
    c = client(path)

    def register(dest, token, received="2026-11-05"):
        return c.post("/enquiries/new", follow_redirects=True, data={
            "received_on": received, "requester_party_id": str(p["office"]), "service_mode": "Air",
            "gross_kg": "300", "gross_unit": "kg", "origin_country": "USA", "dest_country": dest,
            "decision": "new", "form_token": token}).data.decode("utf-8")
    assert "scope Out" not in register("China", "t1")
    assert "scope Out" not in register("Chile", "t2")
    assert "scope Out" not in register("China", "t3", received="2026-09-20")
    got = dict(conn.execute("SELECT opp_no, scope FROM enquiry").fetchall())
    assert got == {"OPP-9151": "In", "OPP-9152": "In", "OPP-9153": "In"}


# ------------------------------------------------------------ the command --
@pytest.fixture
def mixed(world):
    conn, repo, p, case, path = world
    got = {"office": case("office"), "office_latam": case("office", dest="Brazil"),
           "office_sept": case("office", received="2026-09-15"), "direct": case("direct"),
           "office_hers": case("office", origin="China", dest="USA"), "agent": case("agent")}
    hand = db.Repository(conn, source="ui")
    hand.update("enquiry", got["office_hers"], {"scope": "Out"})
    hand.update("enquiry", got["office_hers"], {"scope": "In"})
    hand.commit()
    conn.execute("UPDATE change_log SET at = '2026-11-07 09:00:00' WHERE table_name = 'enquiry' AND row_id = ? "
                 "AND field = 'scope'", (got["office_hers"],))
    conn.commit()
    yield conn, got, path


def test_the_command_lists_direct_customers_and_offices(mixed):
    # SC1: read only, every case the report leaves out, her hand or not
    conn, got, path = mixed
    report = dco.run(conn)
    assert [(r[0], r[4], r[6]) for r in report.rows] == [
        ("OPP-9101", "own office, not LATAM", "Office"), ("OPP-9104", "not LATAM", "Direct Customer"),
        ("OPP-9105", "own office, not LATAM", "Office")]
    text = "\n".join(report.lines())
    assert "  OPP-9101  2026-11-04  JoJo Varnholm  USA to China  New" in text
    assert scope(conn, got["office"]) == "In"


def test_the_command_refuses_apply_under_its_new_name_and_the_old_one(mixed, tmp_path, capsys):
    # SC1: was 'applies'; --apply is refused and nothing is written
    conn, got, path = mixed
    conn.close()
    restore = tmp_path / "restore"
    for name in ("not-latam-out", "direct-customers-out"):
        assert main([name, "--db", str(path), "--apply", "--restore-dir", str(restore)]) == 2
        assert dco.NO_APPLY in capsys.readouterr().out
    assert not restore.exists()
    conn = db.open_db(path)
    try:
        assert {scope(conn, i) for i in got.values()} == {"In"}
    finally:
        conn.close()
    assert main(["direct-customers-out", "--db", str(path)]) == 0
    assert "read only" in capsys.readouterr().out


# ------------------------------------------------------------ the header --
def test_the_h1_carries_the_number_with_its_copy_and_the_sub_line_does_not(world):
    conn, repo, p, case, path = world
    got = case("agent", origin_loc="MIA", dest_loc="NGB")
    body = client(path).get("/enquiries/{}".format(got)).data.decode("utf-8")
    h1 = body[body.index("<h1>"):body.index("</h1>")]
    assert h1.index('data-copy-opp="[OPP-9101] "') < h1.index('class="h-title"')
    assert '<span class="opp-copied" aria-live="polite" hidden>Copied</span></span>' in h1
    sub = body[body.index('<div class="sub">'):body.index("</div>", body.index('<div class="sub">'))]
    assert "OPP-9101" not in sub and "data-copy-opp" not in sub
    assert sub.startswith('<div class="sub">requester ')


# replaces two header tests of tests/test_case_page_v2.py (removed)
def test_a_case_with_no_places_says_its_number_once_and_shows_a_new_cases_tiles(world):
    conn, repo, p, case, path = world
    got = case("agent", origin=None, dest=None)
    body = client(path).get("/enquiries/{}".format(got)).data.decode("utf-8")
    h1 = body[body.index("<h1>"):body.index("</h1>")]
    assert h1.count(">OPP-9101<") == 1 and 'class="h-title"' not in h1 and "h-dot" not in h1
    assert '<span class="pill big s-new">New</span>' in h1
    facts = body[body.index('<div class="facts">'):body.index('<div class="layout">')]
    import re
    assert re.findall(r'<div class="f-l">(.*?)</div>', facts) == [
        "Received", "Age", "Rate requests", "Vendor quote", "Next"]


def test_a_won_case_has_its_status_in_the_h1_and_rate_used_after_the_rate_requests(world):
    conn, repo, p, case, path = world
    got = case("agent", dest="Chile", origin_loc="MIA", dest_loc="SAI")
    repo.set_status(got, "Won", "2026-11-05")
    repo.commit()
    body = client(path).get("/enquiries/{}".format(got)).data.decode("utf-8")
    h1 = body[body.index("<h1>"):body.index("</h1>")]
    assert '<span class="pill big s-won">Won</span>' in h1 and '<span class="h-title">MIA to SAI</span>' in h1
    assert body.index('id="vendors"') < body.index('id="won"') < body.index('id="mail"')
    assert 'id="rate-used-form"' in body
