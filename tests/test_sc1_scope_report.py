"""Batch SC1 (2026-11-10, schema v49): scope by hand only, and the report
decides who counts (two layers); LATAM set by hand; one column per mode in
the month report; Other agents; quote-both cases; legacy rows shown as
Legacy; the team site's latam, legacy and direct customers on the Assign
list; scope-back.

Every company, person, place, number and date here is invented; cases from
OPP-9701, mail on .test domains.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
import sqlite3

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import (assign_list, cheatsheet, dashboard, db, direct_customers_out as dco, firebase_pull as fp,
                     firebase_sync as fs, monthly, monthly_export, report as report_mod, schema_migrations,
                     scope_back, status as status_mod)
from opplist.__main__ import main
from opplist.web import create_app
from test_firebase_sync import FakeFirestore

LATAM = ["Chile", "Brazil", "Mexico"]


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "sc1.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    repo.insert("country", {"name": "Mexico", "region": "Latin America"})
    db.set_setting(conn, "latam_countries", LATAM)
    db.set_setting(conn, "owners", ["Ana", "Ben"])
    db.set_setting(conn, "next_opp_number", "9781")
    p = {"tbc": add_party(repo, "(TBC)", placeholder=1),
         "agent": add_party(repo, "Velloran Freight", country="Chile", mfn_member=1),
         "agent_row": add_party(repo, "Tamsin Cargo Lines", country="USA"),
         "office": add_party(repo, "JoJo Orrinvale", ptype="Office", country="China"),
         "direct": add_party(repo, "Pellbrook Mills", ptype="Direct Customer", country="USA"),
         "direct_latam": add_party(repo, "Quenara Foods", ptype="Direct Customer", country="Brazil"),
         "unknown": add_party(repo, "Unnamed Asker", ptype="Unknown", country="Unknown")}
    repo.commit()
    n = [9701]

    def case(party, received="2026-11-04", origin="USA", dest="China", mode="Air", status="New", **extra):
        opp = "OPP-{}".format(n[0])
        n[0] += 1
        extra.setdefault("scope", "In")
        got = add_enquiry(repo, opp, p[party], received=received, status=status, service_mode=mode,
                          origin_country=origin, dest_country=dest,
                          source_subject="RATE REQUEST // {}".format(opp), **extra)
        repo.commit()
        return got
    yield conn, repo, p, case, path
    conn.close()


def client(path):
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client()


def scope(conn, case_id):
    return conn.execute("SELECT scope FROM enquiry WHERE id = ?", (case_id,)).fetchone()[0]


def opp(conn, case_id):
    return conn.execute("SELECT opp_no FROM enquiry WHERE id = ?", (case_id,)).fetchone()[0]


def counted_opps(conn, month):
    return [r[0] for r in conn.execute(
        "SELECT e.opp_no {} WHERE e.month = ? AND {} ORDER BY e.opp_no".format(monthly.BASE, monthly.CASES),
        (month,))]


# ------------------------------------------------ 1. scope by hand only --
def test_registration_never_sets_scope(world):
    conn, repo, p, case, path = world
    c = client(path)

    def register(party, received, dest, token):
        return c.post("/enquiries/new", follow_redirects=True, data={
            "received_on": received, "requester_party_id": str(p[party]), "service_mode": "Air",
            "gross_kg": "300", "gross_unit": "kg", "origin_country": "USA", "dest_country": dest,
            "decision": "new", "form_token": token}).data.decode("utf-8")
    pages = [register("direct", "2026-09-20", "China", "t1"), register("direct", "2026-11-05", "China", "t2"),
             register("office", "2026-11-05", "China", "t3")]
    assert all("scope Out" not in page for page in pages)
    assert dict(conn.execute("SELECT opp_no, scope FROM enquiry").fetchall()) == {
        "OPP-9781": "In", "OPP-9782": "In", "OPP-9783": "In"}
    # the repository's own create, no scope given
    made = repo.create_enquiry({"requester_party_id": p["direct"], "received_on": "2026-08-02",
                                "origin_country": "USA", "dest_country": "China"}, "2026-08-02")
    repo.commit()
    assert scope(conn, made) == "In"


def test_a_tbc_change_never_sets_scope(world):
    conn, repo, p, case, path = world
    from_site, from_page = case("tbc"), case("tbc")
    action = dict(type="requester", opp=opp(conn, from_site), partyId=p["office"], by="dee@orrin.test",
                  name="Dee Rowan", owner="Ana", app="test", at=dt.datetime(2026, 11, 6, 15, 0, tzinfo=dt.timezone.utc))
    site = db.Repository(conn, source=fp.SOURCE)
    ok, message = fp.take_in(site, "a1", action, "2026-11-06")
    site.commit()
    assert ok and message == "{}: the requester is now JoJo Orrinvale.".format(opp(conn, from_site))
    page = client(path).post("/enquiries/{}".format(from_page), data={"requester_party_id": str(p["direct"])},
                             follow_redirects=True).data.decode("utf-8")
    conn.rollback()
    assert "scope Out" not in page
    assert scope(conn, from_site) == "In" and scope(conn, from_page) == "In"


def test_not_latam_out_only_lists_and_refuses_apply(world, capsys):
    conn, repo, p, case, path = world
    office, direct, before = case("office"), case("direct"), case("direct", received="2026-09-03", dest="Chile")
    latam_office = case("office", dest="Brazil")
    report = dco.run(conn)
    assert [(r[0], r[4]) for r in report.rows] == [(opp(conn, before), dco.BEFORE),
                                                   (opp(conn, office), dco.OWN_OFFICE),
                                                   (opp(conn, direct), dco.NOT_LATAM)]
    conn.close()
    assert main(["not-latam-out", "--db", str(path), "--apply"]) == 2
    assert dco.NO_APPLY in capsys.readouterr().out
    assert main(["direct-customers-out", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "read only" in out and "own office, not LATAM" in out
    conn = sqlite3.connect(path)
    assert {r[0] for r in conn.execute("SELECT scope FROM enquiry")} == {"In"}
    assert latam_office
    conn.close()


# --------------------------------------------------- 2. the report rule --
def test_the_report_rule_case_by_case(world):
    conn, repo, p, case, path = world
    got = {
        "dc_sept": case("direct", received="2026-09-12", dest="Chile"),              # before October: out
        "dc_oct_latam": case("direct", received="2026-10-07", dest="Chile"),         # counted
        "dc_oct_row": case("direct", received="2026-10-08", dest="China"),           # not LATAM: out
        "dc_oct_brazil": case("direct_latam", received="2026-10-09", dest="China"),  # the customer's country
        "dc_oct_no_dest": case("direct", received="2026-10-09", dest=None),          # not decided: counted
        "office_oct_row": case("office", received="2026-10-10", dest="China"),       # out
        "office_oct_latam": case("office", received="2026-10-10", dest="Mexico"),    # counted
        "office_sept_row": case("office", received="2026-09-10", dest="China"),      # before October: counted
        "handled": case("agent_row", received="2026-10-11", handled_by="QXT"),       # another office's: counted
        "yes": case("direct", received="2026-10-12", dest="China", latam_choice="Y"),  # set to yes: counted
        "no": case("office", received="2026-10-13", dest="Brazil", latam_choice="N"),   # set to no: out
        "no_missing": case("direct", received="2026-10-13", dest=None, latam_choice="N"),  # no, a country missing
        "unknown": case("unknown", received="2026-10-14"),                           # not a counted type
        "dropped": case("agent", received="2026-10-15", status="Dropped", closed_reason="Registered in error"),
        "out": case("agent", received="2026-10-16", scope="Out"),                   # hers
    }
    want = {"dc_oct_latam", "dc_oct_brazil", "dc_oct_no_dest", "office_oct_latam", "handled", "yes"}
    assert counted_opps(conn, "2026-10") == sorted(opp(conn, got[k]) for k in want)
    assert counted_opps(conn, "2026-09") == [opp(conn, got["office_sept_row"])]
    assert dco.reason_of(conn, got["no"]) == dco.OWN_OFFICE and dco.reason_of(conn, got["yes"]) == ""
    assert dco.reason_of(conn, got["no_missing"]) == dco.NOT_LATAM
    # section 1's note: of the counted cases, the ones another office handles
    r = monthly.build(conn, "2026-10", dt.date(2026, 11, 10))
    assert r.by_scope.rows[-1].label == "Of which handled by our other offices: 1 (QXT 1)"
    # report.py and the dashboard read the same rule
    rp = report_mod.build(conn, "2026-10", "2026-11-10")
    assert dict((row[0], row[1]) for row in rp.sections[0].rows)["Total"] == len(want)
    dash_ids = {row["id"] for row in dashboard.base_rows(conn)}
    assert {got[k] for k in want} <= dash_ids and not {got[k] for k in ("dc_sept", "dc_oct_row", "no", "out")} & dash_ids
    assert got["dropped"] in dash_ids                              # the Dropped slice keeps it


def test_the_python_rule_and_the_sql_agree_on_every_case(world):
    conn, repo, p, case, path = world
    parties = ("agent", "agent_row", "office", "direct", "direct_latam", "unknown")
    days = ("2026-09-30", "2026-10-01", None)
    lanes = (("USA", "China"), ("USA", "Chile"), ("Unknown", "China"), ("USA", None))
    choices = (None, "Y", "N")
    states = (("In", "Quoted", 0, None), ("In", "Dropped", 1, None), ("In", "Dropped", 0, "Duplicate of OPP-9702"),
              ("Out", "Won", 0, None))
    for party, day, (origin, dest), choice, (sc, st, legacy, why) in itertools.product(
            parties, days, lanes, choices, states):
        made = case(party, received="2026-10-02", origin=origin, dest=dest, latam_choice=choice, scope=sc,
                    status=st, legacy=legacy, closed_reason=why)
        if day is None:
            repo.update("enquiry", made, {"received_on": None})
        else:
            repo.update("enquiry", made, {"received_on": day})
    repo.commit()
    rows = conn.execute(
        "SELECT e.*, p.type AS ptype, {out} AS sql_reason, {counted} AS sql_counted, {volume} AS sql_volume "
        "FROM v_enquiry e JOIN party p ON p.id = e.requester_party_id".format(
            out=dco.out_sql(), counted=dco.counted_sql(), volume=dco.counted_sql(volume=True))).fetchall()
    assert len(rows) == len(parties) * len(days) * len(lanes) * len(choices) * len(states)
    for r in rows:
        py = dco.out_reason(r["ptype"], r["received_on"], r["origin_country"], r["dest_country"], r["latam"],
                            r["latam_choice"])
        assert r["sql_reason"] == py, dict(r)
        args = (r["scope"], r["status"], r["ptype"], r["received_on"], r["origin_country"], r["dest_country"],
                r["latam"], r["latam_choice"])
        assert bool(r["sql_counted"]) == dco.counted(*args), dict(r)
        assert bool(r["sql_volume"]) == dco.counted(*args, legacy=r["legacy"], closed_reason=r["closed_reason"],
                                                    volume=True), dict(r)


# --------------------------------------------------- 3. LATAM by hand --
def test_the_latam_chip_and_more(world):
    conn, repo, p, case, path = world
    lane, row = case("agent_row", dest="Chile"), case("agent_row", dest="China")
    c = client(path)

    def page(case_id):
        return c.get("/enquiries/{}".format(case_id)).data.decode("utf-8")
    assert '<span class="pill latam-chip" id="latam-chip">LATAM</span>' in page(lane)
    assert 'id="latam-chip"' not in page(row)
    assert 'id="latam-choice"' in page(row) and ">automatic</button>" in page(row)
    c.post("/enquiries/{}/latam".format(row), data={"latam": "Y"})
    assert '<span class="pill latam-chip" id="latam-chip">LATAM, set by hand</span>' in page(row)
    c.post("/enquiries/{}/latam".format(lane), data={"latam": "N"})
    assert 'id="latam-chip"' not in page(lane)
    listed = c.get("/enquiries?view=all").data.decode("utf-8")
    assert '<span class="lbl fill latam-chip">LATAM, set by hand</span>' in listed
    conn.rollback()
    assert conn.execute("SELECT latam FROM v_enquiry WHERE id = ?", (lane,)).fetchone()[0] == "N"
    assert conn.execute("SELECT latam FROM v_route WHERE enquiry_id = ?", (row,)).fetchone()[0] == "Y"
    logged = conn.execute("SELECT row_id, old_value, new_value FROM change_log WHERE table_name = 'enquiry' "
                          "AND field = 'latam_choice' ORDER BY id").fetchall()
    assert [tuple(x) for x in logged] == [(row, None, "Y"), (lane, None, "N")]
    c.post("/enquiries/{}/latam".format(lane), data={"latam": "auto"})
    conn.rollback()
    assert tuple(conn.execute("SELECT e.latam_choice, v.latam FROM enquiry e JOIN v_enquiry v ON v.id = e.id "
                              "WHERE e.id = ?", (lane,)).fetchone()) == (None, "Y")


def test_the_v49_upgrade_adds_the_column_moves_the_three_parties_once(world, monkeypatch):
    conn, repo, p, case, path = world
    names = ("Harlow Spindle", "Oakmere Parcel", "Brisk Tern")
    ids = [add_party(repo, name, country="USA") for name in names]
    keep = add_party(repo, "Harlow Spindle Two", country="USA")
    case("agent_row", latam_choice="Y")
    repo.commit()
    conn.close()
    # a v48 database: enquiry without the column (rebuilt from its own CREATE)
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = OFF")
    for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        raw.execute("DROP VIEW {}".format(name))
    create = re.sub(r"--[^\n]*", "", raw.execute("SELECT sql FROM sqlite_master WHERE name = 'enquiry'").fetchone()[0])
    create = re.sub(r",\s*latam_choice[^\n]*", "", create).replace("CREATE TABLE enquiry", "CREATE TABLE enquiry_v48", 1)
    columns = [r[1] for r in raw.execute("PRAGMA table_info(enquiry)") if r[1] != "latam_choice"]
    raw.execute(create)
    raw.execute("INSERT INTO enquiry_v48 ({c}) SELECT {c} FROM enquiry".format(c=", ".join(columns)))
    raw.execute("DROP TABLE enquiry")
    raw.execute("ALTER TABLE enquiry_v48 RENAME TO enquiry")
    assert "latam_choice" not in [r[1] for r in raw.execute("PRAGMA table_info(enquiry)")]
    raw.execute("UPDATE setting SET value = '48' WHERE key = 'schema_version'")
    raw.execute("UPDATE setting SET value = ? WHERE key = 'closed_reasons_lost'",
                ('["Price too high", "Other"]',))
    raw.commit()
    raw.close()
    monkeypatch.setattr(schema_migrations, "_V49_DIRECT_CUSTOMERS", names)
    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    try:
        assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION >= 49
        assert "enquiry.latam_choice added" in notes
        assert "party type Agent -> Direct Customer: Harlow Spindle, Oakmere Parcel, Brisk Tern" in notes
        assert {r[0] for r in conn.execute("SELECT type FROM party WHERE id IN (?, ?, ?)", ids)} == {"Direct Customer"}
        assert conn.execute("SELECT type FROM party WHERE id = ?", (keep,)).fetchone()[0] == "Agent"
        logged = conn.execute("SELECT DISTINCT action_id, source FROM change_log WHERE table_name = 'party' "
                              "AND field = 'type'").fetchall()
        assert len(logged) == 1 and logged[0]["source"] == "migration"
        assert status_mod.closed_reasons(conn, "Lost") == ["Price too high", status_mod.OTHER_MODE_REASON, "Other"]
        last = conn.execute("SELECT MAX(id) FROM change_log").fetchone()[0]
        again = schema_migrations._to_v49(conn)
        assert any(n.startswith("left as they are: ") for n in again)
        assert conn.execute("SELECT MAX(id) FROM change_log").fetchone()[0] == last
        assert status_mod.closed_reasons(conn, "Lost").count(status_mod.OTHER_MODE_REASON) == 1
    finally:
        conn.close()


# --------------------------------------------------------- 4. scope-back --
@pytest.fixture
def outs(world):
    conn, repo, p, case, path = world
    got = {"before": case("direct", received="2026-09-11", status="Quoted"),
           "before_new": case("direct", received="2026-09-12"),
           "oct_row": case("direct", received="2026-10-14"),
           "office_reg": case("office", received="2026-10-20", scope="Out"),       # Out from registration
           "maybe": case("agent", received="2026-10-21", scope="Out"),             # no rule reason now
           "hers": case("direct", received="2026-10-22"),
           "agent_in": case("agent", received="2026-10-23")}
    rules = db.Repository(conn, source="migration")
    for key in ("before", "before_new", "oct_row"):
        rules.update("enquiry", got[key], {"scope": "Out"})
    rules.commit()
    hand = db.Repository(conn, source="ui")
    hand.update("enquiry", got["hers"], {"scope": "Out"})
    hand.commit()
    yield conn, got, path


def test_scope_back_lists_the_rules_out_and_leaves_hers(outs):
    conn, got, path = outs
    found = scope_back.find(conn)
    assert [i.enquiry_id for i in found.rules] == [got[k] for k in ("before", "before_new", "oct_row", "office_reg")]
    assert [i.part for i in found.rules] == [scope_back.DC_BEFORE, scope_back.DC_BEFORE, scope_back.DC_FROM,
                                             scope_back.OFFICES]
    assert [i.enquiry_id for i in found.maybe] == [got["maybe"]]
    assert [i.enquiry_id for i in found.hers] == [got["hers"]]
    assert [i.enquiry_id for i in found.open_cases()] == [got["before"], got["before_new"]]
    text = "\n".join(found.lines())
    assert "still open from before October" in text and "(New 1, Quoted 1)" in text
    assert "<< open, back on Tracking and the Assign List" in text
    assert {scope(conn, i) for i in got.values() if i != got["agent_in"]} == {"Out"}


def test_scope_back_does_not_mark_an_open_case_it_leaves_out(world):
    conn, repo, p, case, path = world
    hers = case("direct", received="2026-09-15", status="Quoted")
    hand = db.Repository(conn, source="ui")
    hand.update("enquiry", hers, {"scope": "Out"})
    hand.commit()
    found = scope_back.find(conn)
    assert [i.enquiry_id for i in found.hers] == [hers] and found.rules == []
    assert "back on Tracking" not in "\n".join(found.lines())


def test_scope_back_apply_sets_only_the_rules_cases_in_one_action(outs, tmp_path, capsys):
    conn, got, path = outs
    conn.close()
    restore = tmp_path / "restore"
    assert main(["scope-back", "--db", str(path), "--apply", "--restore-dir", str(restore)]) == 0
    assert "applied" in capsys.readouterr().out
    assert list(restore.glob("opplist_before_scope-back_*.db"))
    conn = db.open_db(path)
    try:
        scopes = {k: scope(conn, i) for k, i in got.items()}
        assert scopes == {"before": "In", "before_new": "In", "oct_row": "In", "office_reg": "In",
                          "maybe": "Out", "hers": "Out", "agent_in": "In"}
        logged = conn.execute("SELECT DISTINCT action_id, source FROM change_log WHERE table_name = 'enquiry' "
                              "AND field = 'scope' AND new_value = 'In'").fetchall()
        assert len(logged) == 1 and logged[0]["source"] == "migration"
        # back In, the report still leaves them out; the New one is on the Assign list
        assert counted_opps(conn, "2026-09") == []
        assert got["before_new"] in {r.enquiry_id for r in assign_list.listed(conn, "2026-11-10")}
        assert scope_back.find(conn).rules == []
    finally:
        conn.close()


def test_a_case_back_in_is_pushed_again(outs, tmp_path):
    conn, got, path = outs
    conn.close()
    fake = FakeFirestore()
    kw = dict(client=fake, state_file=tmp_path / "fs.json", reports_dir=tmp_path / "reports",
              record=tmp_path / "exports.json", today="2026-11-10")
    conn = db.open_db(path)
    case_opp = opp(conn, got["oct_row"])
    # it was on the site before the rule set it Out: written, then gone
    db.Repository(conn, source="ui").update("enquiry", got["oct_row"], {"scope": "In"})
    conn.commit()
    conn.close()
    fs.sync(path, **kw)
    conn = db.open_db(path)
    db.Repository(conn, source="migration").update("enquiry", got["oct_row"], {"scope": "Out"})
    conn.commit()
    conn.close()
    fs.sync(path, **kw)
    assert fake.docs["cases/" + case_opp] == {"opp": case_opp, "gone": True}
    assert main(["scope-back", "--db", str(path), "--apply", "--no-backup"]) == 0
    fs.sync(path, **kw)
    assert fake.docs["cases/" + case_opp].get("gone") is None and fake.docs["cases/" + case_opp]["opp"] == case_opp
    assert case_opp in {row["opp"] for row in fake.docs["lists/inquiries"]["rows"]}


# --------------------------------------------- 5. one column per mode --
def _month(case, modes, month="2026-12"):
    out = []
    for i, mode in enumerate(modes):
        out.append(case("agent", received="{}-{:02d}".format(month, 2 + i), origin="USA", dest="Chile", mode=mode,
                        status="Quoted"))
    return out


def test_a_month_without_the_new_modes_keeps_section_1(world):
    conn, repo, p, case, path = world
    _month(case, ["Air", "Air", "FCL", "LCL", None])
    r = monthly.build(conn, "2026-12", dt.date(2027, 1, 5))
    assert r.by_scope.columns == ["Scope / requester", "Total", "Air", "FCL", "LCL", "Won", "Lost"]
    assert [(row.label, row.cells) for row in r.by_scope.rows[:3]] == [
        ("LATAM", ["5", "2", "1", "1", "0", "0"]), ("LATAM agents, MFN", ["5", "2", "1", "1", "0", "0"]),
        ("Rest of world", ["0", "0", "0", "0", "0", "0"])]
    assert r.s1_class == "" and r.kpis[2] == ("2 / 2", "Air / Ocean")
    assert 'class="mid s1"' in monthly_export.render_html(r)


def test_the_new_modes_have_their_columns_and_roro_is_ocean(world):
    conn, repo, p, case, path = world
    _month(case, ["Air", "Sea-Air", "FCL", "LCL", "RORO", "Truck", "Cross-border Truck", "Project", "Not specified"])
    r = monthly.build(conn, "2026-12", dt.date(2027, 1, 5))
    assert r.by_scope.columns == ["Scope / requester", "Total"] + list(monthly.MODE_COLUMNS) + ["Won", "Lost"]
    assert r.by_scope.rows[0].cells == ["9", "1", "1", "1", "1", "1", "1", "1", "1", "0", "0"]
    # KPI: Air 1, Ocean = FCL + LCL + RORO, Other = Sea-Air, Truck, Cross-border Truck, Project
    assert r.kpis[2] == ("1 / 3 / 4", monthly.OTHER_LABEL)
    # section 6 counts routes the same way
    got = {row.label: row.cells for row in r.direction.rows}
    assert got["Ocean"][-1] == "3" and got["Other"][-1] == "4"
    assert r.s1_class == " m6"
    html = monthly_export.render_html(r)
    assert "Cross-border<br>Truck" in html and 'class="mid s1 m6"' in html
    sheet = cheatsheet.build(conn, "2026-12")
    assert sheet.glance["other"] == 5 and "RORO 1" in sheet.glance["other_parts"]
    # report.py's mode: Ocean takes RORO
    mode = report_mod.mode(conn, "2026-12")
    assert dict((row[0], row[1:]) for row in mode.rows)["Total"] == (1, 3, 5, 9)


def test_five_mode_columns_take_the_m5_class(world):
    conn, repo, p, case, path = world
    _month(case, ["Air", "FCL", "LCL", "Sea-Air"])
    r = monthly.build(conn, "2026-12", dt.date(2027, 1, 5))
    assert r.by_scope.columns[2:-2] == ["Air", "Sea-Air", "FCL", "LCL"] and r.s1_class == ""
    case("agent", received="2026-12-20", dest="Chile", mode="Truck")
    r = monthly.build(conn, "2026-12", dt.date(2027, 1, 5))
    assert r.s1_class == " m5"


# ------------------------------------------------------ 6. Other agents --
def test_other_agents_in_every_section(world):
    conn, repo, p, case, path = world
    case("agent", received="2026-12-02", dest="Chile")
    case("agent_row", received="2026-12-03", origin="USA", dest="China")
    case("agent_row", received="2026-11-03", origin="USA", dest="China")
    r = monthly.build(conn, "2026-12", dt.date(2027, 1, 5))
    assert "Other agents" in [row.label for row in r.by_scope.rows]
    assert ("Other agents", "head") in [(row.label, row.cls) for row in r.top_agents.rows]
    assert "Other agents" in r.weeks["series"] and "Other agents" in [m[0] for m in r.mix]
    assert dict(monthly.FOUR)["Other agents"] == "#83cd48"
    text = "\n".join(monthly.render_text(r))
    assert "Non-LATAM" not in text and "Other agents" in text
    assert "Non-LATAM" not in monthly_export.render_html(r)
    rp = report_mod.build(conn, "2026-12", "2027-01-05")
    assert "Other agents" in [row[0] for row in rp.sections[0].rows]


# ---------------------------------------------------- 7. quote-both cases --
def _quote_both(conn, repo, case, first="Air", second="FCL", won="B"):
    made = case("agent", received="2026-12-04", dest="Chile", mode=first)
    repo.add_route(made, {"origin_country": "USA", "dest_country": "Chile", "service_mode": second})
    repo.commit()
    a, b = [r[0] for r in conn.execute("SELECT id FROM enquiry_route WHERE enquiry_id = ? AND voided = 0 "
                                       "ORDER BY seq", (made,))]
    winner, loser = (b, a) if won == "B" else (a, b)
    repo.update("enquiry_route", winner, {"result": "Won", "result_on": "2026-12-10"})
    repo.update("enquiry_route", loser, {"result": "Lost", "result_on": "2026-12-10",
                                         "lost_reason": status_mod.OTHER_MODE_REASON})
    repo.set_status(made, "Won", "2026-12-10", rate_used="own")
    repo.commit()
    return made


def test_a_quote_both_case_counts_under_its_won_route_and_never_as_lost(world):
    conn, repo, p, case, path = world
    made = _quote_both(conn, repo, case)
    r = monthly.build(conn, "2026-12", dt.date(2027, 1, 5))
    cols = r.by_scope.columns
    total = dict(zip(cols[1:], r.by_scope.rows[0].cells))
    assert total["FCL"] == "1" and total["Air"] == "0" and total["Won"] == "1" and total["Lost"] == "0"
    assert r.lost_bars == [] and dict((label, value) for value, label in r.kpis)["Lost"] == "0"
    # the routes count each under its own mode (section 6)
    got = {row.label: row.cells[-1] for row in r.direction.rows}
    assert got["Air"] == "1" and got["Ocean"] == "1"
    dash = dashboard.build(conn, "2026-12-20")
    assert status_mod.OTHER_MODE_REASON not in dict(dash.lost)
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (made,)).fetchone()[0] == "Won"
    assert status_mod.OTHER_MODE_REASON in status_mod.closed_reasons(conn, "Lost")


def test_without_a_won_route_route_a_s_mode(world):
    conn, repo, p, case, path = world
    made = case("agent", received="2026-12-04", dest="Chile", mode="Air", status="Quoted")
    repo.add_route(made, {"origin_country": "USA", "dest_country": "Chile", "service_mode": "LCL"})
    repo.commit()
    r = monthly.build(conn, "2026-12", dt.date(2027, 1, 5))
    total = dict(zip(r.by_scope.columns[1:], r.by_scope.rows[0].cells))
    assert total["Air"] == "1" and total["LCL"] == "0"


def test_the_multi_mode_hint_only_on_a_multi_mode_case_with_one_route(world):
    conn, repo, p, case, path = world
    single, other = case("agent", mode="Multi-mode"), case("agent", mode="Air")
    double = case("agent", mode="Multi-mode")
    repo.add_route(double, {"origin_country": "USA", "dest_country": "China", "service_mode": "FCL"})
    repo.commit()
    c = client(path)
    hint = "Quoting more than one mode? Add a route for each mode."
    assert hint in c.get("/enquiries/{}".format(single)).data.decode("utf-8")
    assert hint not in c.get("/enquiries/{}".format(other)).data.decode("utf-8")
    assert hint not in c.get("/enquiries/{}".format(double)).data.decode("utf-8")


# --------------------------------------------------------------- 8. Legacy --
def test_legacy_rows_show_legacy_and_the_dropped_filter_has_none(world):
    conn, repo, p, case, path = world
    old = case("agent", received="2026-03-04", status="Dropped", legacy=1)
    dup = case("agent", received="2026-10-04", status="Dropped", closed_reason="Duplicate of OPP-9702")
    c = client(path)
    listed = c.get("/enquiries?view=all").data.decode("utf-8")
    assert 'data-id="{}" data-status="Legacy"'.format(old) in listed
    assert 'data-id="{}" data-status="Dropped"'.format(dup) in listed
    assert ('<span class="lbl fill s-legacy" title="Imported from the old workbook; outcome not recorded">Legacy</span>'
            in listed)
    assert '<span class="lbl tone-grey">legacy</span>' not in listed
    page = c.get("/enquiries/{}".format(old)).data.decode("utf-8")
    assert page.count('s-legacy" title="Imported from the old workbook; outcome not recorded">Legacy</span>') == 2
    assert '<span class="pill muted-pill">legacy</span>' not in page
    assert '"Legacy": "Imported from the old workbook; outcome not recorded"' in listed
    conn.rollback()
    assert conn.execute("SELECT status FROM enquiry WHERE id = ?", (old,)).fetchone()[0] == "Dropped"
    # section 3 still counts it (it asks how much came in)
    r = monthly.build(conn, "2026-10", dt.date(2026, 11, 10))
    assert r.per_month["latam"][2] + r.per_month["row"][2] == 1


# ------------------------------------------------------------ 9. the site --
def test_the_site_rows_carry_latam_and_legacy_and_assign_lists_direct_customers(world):
    conn, repo, p, case, path = world
    latam = case("agent", dest="Chile")
    row = case("agent_row", dest="China")
    chosen = case("agent_row", dest="China", latam_choice="Y")
    old = case("agent_row", received="2026-04-04", status="Dropped", legacy=1)
    direct = case("direct", dest="China")
    built = fs.build(conn, "2026-11-10")
    rows = {r["opp"]: r for r in built.inquiries}
    assert rows[opp(conn, latam)]["latam"] is True and "latam" not in rows[opp(conn, row)]
    assert rows[opp(conn, chosen)]["latam"] is True
    assert rows[opp(conn, old)]["legacy"] is True and rows[opp(conn, old)]["st"] == "Dropped"
    assert "legacy" not in rows[opp(conn, row)]
    assert built.cases[opp(conn, latam)]["latam"] is True and built.cases[opp(conn, old)]["legacy"] is True
    waiting = [w["opp"] for w in built.assign["waiting"]]
    assert opp(conn, direct) in waiting
