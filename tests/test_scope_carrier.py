"""Schema v9: enquiry.scope (In / Out), party type Carrier, rate used
'Carrier direct', pale status pills, and the analysis columns."""

from __future__ import annotations

import csv
import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, export_analysis, paths, report, schema_migrations, tracking


TODAY = "2026-09-10"


@pytest.fixture
def book(repo):
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile", mfn_member=1)
    harlow = add_party(repo, "Harlow Fasteners", ptype="Direct Customer", country="USA")
    atlas = add_party(repo, "Atlas Air", ptype="Carrier", country="USA")
    seavio = add_party(repo, "Seavio", ptype="Unknown", country="USA")
    inbound = add_enquiry(repo, "OPP-0001", tolvex, received="2026-08-03", status="New",
                          origin_loc="MIA", origin_alt="2215 Harbor Rd", dest_loc="SAI")
    out = add_enquiry(repo, "OPP-0002", harlow, received="2026-08-04", status="New",
                      scope="Out")
    for case in (inbound, out):
        repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": seavio,
                                       "asked_on": "2026-08-05"})
    repo.commit()
    return repo, {"in": inbound, "out": out, "atlas": atlas, "harlow": harlow,
                  "tolvex": tolvex, "seavio": seavio}


# ------------------------------------------------------------- schema --
def test_a_carrier_is_a_party_type_and_a_rate_used(book):
    repo, ids = book
    assert schema_migrations.current_version(repo.conn) == schema_migrations.CURRENT_VERSION
    repo.set_rate_used(ids["in"], "carrier", party_id=ids["atlas"])
    row = repo.conn.execute("SELECT rate_used, awarded_vendor_id FROM enquiry WHERE id = ?",
                            (ids["in"],)).fetchone()
    assert tuple(row) == ("carrier", ids["atlas"])
    repo.set_rate_used(ids["in"], "carrier")            # a courier not on file
    assert repo.conn.execute("SELECT awarded_vendor_id FROM enquiry WHERE id = ?",
                             (ids["in"],)).fetchone()[0] is None


def test_upgrading_a_v8_database_widens_the_checks_and_keeps_every_row(tmp_path):
    """A real v8 file: the old CHECK lists, no scope column. Every party,
    case, vendor request and event must come through the rebuild."""
    old_sql = paths.SCHEMA_SQL.read_text(encoding="utf-8")
    old_sql = old_sql.replace("'Direct Customer','Unknown','Carrier'", "'Direct Customer','Unknown'")
    old_sql = old_sql.replace("'own','not_recorded','carrier'", "'own','not_recorded'")
    old_sql = re.sub(r"    scope +TEXT NOT NULL DEFAULT 'In' CHECK \(scope IN \('In','Out'\)\),\n"
                     r"(?:.*?\n)*?(?=    awarded_vendor_id)", "", old_sql)
    schema_path = tmp_path / "old.sql"
    schema_path.write_text(old_sql, encoding="utf-8")
    import sqlite3
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(old_sql)
    assert "scope" not in {r[1] for r in conn.execute("PRAGMA table_info(enquiry)")}
    conn.execute("INSERT INTO country (name, region) VALUES ('USA', 'North America')")
    conn.execute("INSERT INTO party (key, type, country) VALUES ('Harlow', 'Direct Customer', 'USA')")
    conn.execute("INSERT INTO party (key, type, country) VALUES ('Tolvex', 'Agent', 'USA')")
    conn.execute("INSERT INTO enquiry (opp_no, requester_party_id, received_on, status) "
                 "VALUES ('OPP-0001', 1, '2026-08-01', 'New')")
    conn.execute("INSERT INTO enquiry (opp_no, requester_party_id, received_on, status) "
                 "VALUES ('OPP-0002', 2, '2026-08-02', 'Won')")
    conn.execute("INSERT INTO vendor_request (enquiry_id, vendor_party_id, asked_on) VALUES (2, 2, '2026-08-03')")
    conn.execute("INSERT INTO status_event (enquiry_id, at, status) VALUES (2, '2026-08-02', 'Won')")
    conn.execute("UPDATE setting SET value = '8' WHERE key = 'schema_version'")
    conn.commit()
    with pytest.raises(Exception):
        conn.execute("INSERT INTO party (key, type, country) VALUES ('Atlas', 'Carrier', 'USA')")
    conn.rollback()

    notes = schema_migrations.upgrade(conn)
    assert schema_migrations.current_version(conn) == schema_migrations.CURRENT_VERSION
    assert any("scope set to Out on 1 case(s)" in n and "OPP-0001" in n for n in notes)
    assert any("party rebuilt" in n for n in notes) and any("enquiry rebuilt" in n for n in notes)
    conn.execute("INSERT INTO party (key, type, country) VALUES ('Atlas', 'Carrier', 'USA')")
    assert conn.execute("SELECT COUNT(*) FROM enquiry").fetchone()[0] == 2
    assert conn.execute("SELECT scope FROM enquiry WHERE opp_no = 'OPP-0001'").fetchone()[0] == "Out"
    assert conn.execute("SELECT scope FROM enquiry WHERE opp_no = 'OPP-0002'").fetchone()[0] == "In"
    assert conn.execute("SELECT COUNT(*) FROM vendor_request").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == 1
    assert conn.execute("SELECT requester_key FROM v_enquiry WHERE opp_no = 'OPP-0002'"
                        ).fetchone()[0] == "Tolvex"
    conn.execute("UPDATE enquiry SET rate_used = 'carrier' WHERE opp_no = 'OPP-0002'")
    assert {r[1] for r in conn.execute("PRAGMA index_list(party)")} >= {"ux_party_key_ci"}
    conn.close()


def test_the_migration_reads_a_carrier_won_as_carrier_direct(book):
    repo, ids = book
    repo.conn.execute("UPDATE enquiry SET status = 'Won', rate_used = 'agent', "
                      "awarded_vendor_id = ? WHERE id = ?", (ids["atlas"], ids["in"]))
    repo.commit()
    notes = schema_migrations._to_v9(repo.conn)
    assert any("'agent' -> 'carrier' on 1 case" in n for n in notes)
    assert repo.conn.execute("SELECT rate_used FROM enquiry WHERE id = ?",
                             (ids["in"],)).fetchone()[0] == "carrier"


# --------------------------------------------------------------- scope --
def test_out_cases_leave_tracking_the_report_but_not_the_vendor_counts(book):
    repo, ids = book
    sections = tracking.run(repo.conn, today="2026-09-10")
    labels = {item.label for section in sections for item in section.items}
    assert "OPP-0001" in labels and "OPP-0002" not in labels

    built = report.build(repo.conn, "2026-08", today=TODAY)
    inbound = {r[0]: r for r in built.sections[0].rows}
    assert inbound["Total"][1] == 1 and inbound["Direct customers"][1] == 0
    sent = {r[0]: r for r in next(b for b in built.sections if b.title.startswith("5a")).rows}
    assert sent["Total"][1] == 2                        # both vendor requests count


def test_the_analysis_file_carries_scope_and_the_place_details(book, tmp_path):
    repo, ids = book
    export_analysis.run(repo.conn, out_dir=tmp_path, today=TODAY)
    rows = {r["opp_no"]: r for r in csv.DictReader(
        (tmp_path / "enquiries.csv").read_text(encoding="utf-8-sig").splitlines())}
    assert "scope" in rows["OPP-0001"]
    assert (rows["OPP-0001"]["scope"], rows["OPP-0002"]["scope"]) == ("In", "Out")
    assert rows["OPP-0001"]["origin_detail"] == "MIA / 2215 Harbor Rd"
    assert rows["OPP-0001"]["dest_detail"] == "SAI"
    assert rows["OPP-0002"]["origin_detail"] == ""


# ----------------------------------------------------------- the pills --
def test_status_pills_are_a_pale_wash_with_the_colour_as_text():
    css = (paths.PROJECT_DIR / "opplist" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    quoted = re.search(r"\.s-quoted \{([^}]*)\}", css).group(1)
    assert "rgba(42,120,214,.15)" in quoted and "color: #2A78D6" in quoted
    assert "border: 0" in quoted
    for name, rgb, hexcode in (("won", "0,131,0", "#008300"), ("lost", "227,73,72", "#E34948"),
                               ("sourcing", "237,161,0", "#EDA100")):
        rule = re.search(r"\.s-%s \{([^}]*)\}" % name, css).group(1)
        assert "rgba({},.15)".format(rgb) in rule and "color: {}".format(hexcode) in rule
