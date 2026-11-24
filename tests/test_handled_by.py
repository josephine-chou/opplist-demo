"""Cases handled by another of our offices (v31, 2026-09-25):
enquiry.handled_by holds the office's code, NULL is JJC. The status label
reads 'Handled by JJV' in place of New or Quoted, the chasing rules and
JJC's own numbers leave the case out, the data rules keep it, and
Register from Outlook prefills the office when an agent wrote to it with
JJC only copied."""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

import enquiries_client
from conftest import add_enquiry, add_party, seed_reference
from opplist import dashboard, db, intake, monthly, paths, schema_migrations, tracking
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

TODAY = "2026-09-24"          # a Thursday
STEEL = "tone-steel handled"


def tolvex_to_jjv() -> MailInfo:
    """Tolvex's mail to JJV-Kevin Zhao, #JJC-MFN in To (an alias)."""
    with open(paths.FIXTURES_DIR / "agent_request_to_other_office.json", encoding="utf-8") as handle:
        data = json.load(handle)
    return MailInfo(
        subject=data["subject"], sender_name=data["sender_name"],
        sender_address=data["sender_address"], sender_internal=data["sender_internal"],
        received_on=data["received_on"], received_at=data["received_at"],
        to_addresses=data["to_addresses"], cc_addresses=data["cc_addresses"],
        display_to=data["display_to"],
        to_names=[p.strip() for p in data["display_to"].split(";") if p.strip()],
        conversation_id=data["conversation_id"], entry_id=data["entry_id"], body_excerpt=data["body"])




@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "v31.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    repo.insert("country", {"name": "Canada", "region": "North America"})
    tolvex = add_party(repo, "Tolvex", country="Chile")
    klx = add_party(repo, "KLX", country="USA")
    seavio = add_party(repo, "Seavio", country="USA")
    add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    add_party(repo, "JoJo JJV", ptype="Office", country="Canada")
    add_party(repo, "JoJo JQM", ptype="Office", country="USA")
    add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    repo.insert("party_domain", {"domain": "tolvex.example", "party_id": tolvex})
    ids = {"tolvex": tolvex, "klx": klx}

    def case(opp, party, received, **extra):
        values = dict(service_mode="FCL", origin_loc="MIA", origin_country="USA",
                      dest_loc="SAI", dest_country="Chile")
        values.update(extra)
        return add_enquiry(repo, opp, party, received=received, **values)

    ids["jjc_new"] = case("OPP-1701", tolvex, "2026-09-10")                    # JJC's, New
    ids["jjv_new"] = case("OPP-1702", tolvex, "2026-09-10", handled_by="JJV")  # JJV's, New
    ids["jjv_quoted"] = case("OPP-1703", tolvex, "2026-09-02", handled_by="JJV")
    repo.set_status(ids["jjv_quoted"], "Quoted", "2026-09-22")
    ids["jjv_won"] = case("OPP-1704", klx, "2026-09-03", handled_by="JJV")
    repo.set_status(ids["jjv_won"], "Quoted", "2026-09-05")
    repo.set_status(ids["jjv_won"], "Won", "2026-09-15")                       # R6 bait
    repo.insert("vendor_request", {"enquiry_id": ids["jjv_won"], "vendor_party_id": seavio,
                                   "asked_on": "2026-09-04", "replied_on": "2026-09-04"})
    ids["jqm_vendor"] = case("OPP-1705", tolvex, "2026-09-08", handled_by="JQM")
    repo.insert("vendor_request", {"enquiry_id": ids["jqm_vendor"], "vendor_party_id": seavio,
                                   "asked_on": "2026-09-09"})                  # R2 bait
    ids["jqm_answered"] = case("OPP-1706", tolvex, "2026-09-11", handled_by="JQM")
    repo.insert("vendor_request", {"enquiry_id": ids["jqm_answered"], "vendor_party_id": seavio,
                                   "asked_on": "2026-09-12", "replied_on": "2026-09-15"})  # R9 bait
    # a lane that needs fixing: the Chilean side a country only (R8)
    ids["jjv_lane"] = case("OPP-1707", tolvex, "2026-09-12", handled_by="JJV", dest_loc=None)
    repo.commit()
    conn.close()
    import opplist.web as web_module
    monkeypatch.setattr(web_module, "_today", lambda: TODAY)
    app = create_app(path)
    app.config["TESTING"] = True
    return app.test_client(), path, ids


def html(client, url) -> str:
    return client.get(url).data.decode("utf-8")


# -------------------------------------------------------------- migration --
def test_v31_adds_the_column_after_a_restore_point_and_keeps_every_view(tmp_path):
    path = tmp_path / "v30.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    add_enquiry(repo, "OPP-0001", add_party(repo, "Tolvex", country="Chile"), received="2026-09-01")
    repo.commit()
    # back to a v30 file: no handled_by, v_enquiry without it
    conn.execute("DROP VIEW v_enquiry")
    conn.execute("DROP VIEW IF EXISTS v_route")             # v33 reads handled_by too
    conn.execute("ALTER TABLE enquiry DROP COLUMN handled_by")
    conn.execute("CREATE VIEW v_enquiry AS SELECT e.* FROM enquiry e")
    conn.execute("UPDATE setting SET value = '30' WHERE key = 'schema_version'")
    conn.commit()
    views = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view'")}
    conn.close()

    notes = []
    conn = db.open_db(path, upgrade_notes=notes)
    current = schema_migrations.CURRENT_VERSION                  # 31 and every step after it
    assert schema_migrations.current_version(conn) == current >= 31
    assert any(n.startswith("restore point opplist_before_schema{}".format(current)) for n in notes), notes
    assert list((tmp_path / "restore-points").glob("opplist_before_schema{}_*.db".format(current)))
    assert "enquiry.handled_by added (NULL = JJC)" in notes
    assert conn.execute("SELECT handled_by FROM v_enquiry").fetchone()[0] is None
    after = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view'")}
    assert set(after) == set(views) | {"v_route"}                 # v33 adds v_route (dropped above)
    assert {k: v for k, v in after.items() if k not in ("v_enquiry", "v_route")} == \
        {k: v for k, v in views.items() if k != "v_enquiry"}
    # re-runnable
    assert schema_migrations._to_v31(conn)[0] == "enquiry.handled_by already on file"
    conn.close()


def test_the_office_codes_come_from_the_own_office_parties(world):
    client, path, ids = world
    conn = db.open_db(path)
    assert db.office_codes(conn) == ["JJV", "JQM"]            # JJC left out
    conn.close()


# ------------------------------------------------------- register draft --
def test_an_agent_mail_to_jjv_with_jjc_copied_is_nwvs_case(world):
    client, path, ids = world
    conn = db.open_db(path)
    mail = tolvex_to_jjv()
    assert intake.handling_office(mail) == "JJV"
    draft = intake.build(conn, mail)
    assert draft.values["handled_by"] == "JJV"
    assert draft.sources["handled_by"] == ""                       # no line under the select
    assert draft.values["requester_party_id"] == ids["tolvex"]       # the agent, not the office
    assert not any("Handled by" in n for n in draft.notes)          # no extra line (11d)
    conn.close()


def test_a_jjc_person_in_to_keeps_the_case_ours():
    mail = tolvex_to_jjv()
    mail.to_names = ["JJV-KEVIN ZHAO", "JJC-IRIS LIN"]
    assert intake.handling_office(mail) == ""
    mail.to_names = ["#JJC-MFN", "Ignacio"]                       # no office at all
    assert intake.handling_office(mail) == ""
    mail.to_names = ["JJV-KEVIN ZHAO"]
    mail.sender_internal = True                                    # we wrote it
    assert intake.handling_office(mail) == ""


def test_the_register_page_shows_handled_by_jjv(world, monkeypatch):
    client, path, ids = world
    from opplist import outlook_bridge
    monkeypatch.setattr(outlook_bridge, "read_selection", tolvex_to_jjv)
    body = html(client, "/enquiries/from-outlook")
    assert '<option value="JJV" selected>JJV</option>' in body
    assert "JJC copied" not in body
    assert re.search(r'name="requester_party_id"[^>]*>.*?<option value="{}"[^>]*selected'.format(ids["tolvex"]),
                     body, re.S) or 'value="{}" selected'.format(ids["tolvex"]) in body


# test_saving_handled_by_from_the_form_and_jjc_stores_null: replaced in 15b by tests/test_cargo_lines_only.py (cargo is lines only).


# ----------------------------------------------------------------- labels --
def test_the_case_page_shows_the_steel_label_in_place_of_new(world):
    client, path, ids = world
    body = html(client, "/enquiries/{}".format(ids["jjv_new"]))
    assert 'class="pill big tone-steel handled">Handled by JJV</span>' in body
    assert 'class="pill big s-new"' not in body
    assert '<option value="JJV" selected>JJV</option>' in body          # the Request card's select
    body = html(client, "/enquiries/{}".format(ids["jjc_new"]))
    assert 'class="pill big s-new">New</span>' in body and STEEL not in body
    assert '<option value="JJC" selected>JJC</option>' in body


def test_a_closed_case_shows_won_after_the_steel_label(world):
    client, path, ids = world
    body = html(client, "/enquiries/{}".format(ids["jjv_won"]))
    assert re.search(r'Handled by JJV</span> <span class="pill big s-won">Won</span>', body)


def test_the_enquiries_status_column_and_the_board(world):
    client, path, ids = world
    body = html(client, "/enquiries?view=all")
    row = re.search(r'<tr class="enq-row" data-id="{}".*?</tr>'.format(ids["jjv_new"]), body, re.S).group(0)
    assert '<span class="lbl fill tone-steel handled">Handled by JJV</span>' in row
    assert 's-new' not in row
    assert '<b>Tolvex</b> <span class="lbl fill tone-steel office">JJV</span>' in row
    won = re.search(r'<tr class="enq-row" data-id="{}".*?</tr>'.format(ids["jjv_won"]), body, re.S).group(0)
    assert re.search(r'Handled by JJV</span> <span class="lbl fill s-won">Won</span>', won)
    card = re.search(r'<div class="kcard" data-id="{}".*?<div class="k2">'.format(ids["jjv_new"]), body, re.S).group(0)
    assert "Handled by JJV" in card
    card = re.search(r'<div class="kcard" data-id="{}".*?<div class="k2">'.format(ids["jjc_new"]), body, re.S).group(0)
    assert "Handled by" not in card
    css = (paths.PACKAGE_DIR / "web" / "static" / "app.css").read_text(encoding="utf-8") \
        if hasattr(paths, "PACKAGE_DIR") else ""
    if css:
        assert ".tone-steel { --tone: #3d6384; }" in css


def test_the_party_page_uses_the_same_label(world):
    client, path, ids = world
    body = html(client, "/parties/{}".format(ids["tolvex"]))
    assert "Handled by JJV" in body


# ------------------------------------------------------------- Enquiries --
def test_open_is_jjc_only_and_other_offices_lists_every_status(world):
    client, path, ids = world
    open_page = enquiries_client.get(client, "/enquiries?view=open")
    assert open_page.shown == ["OPP-1701"]
    others = enquiries_client.get(client, "/enquiries?view=others")
    assert sorted(others.shown) == ["OPP-1702", "OPP-1703", "OPP-1704", "OPP-1705", "OPP-1706", "OPP-1707"]
    assert [k for k, _ in others.groups] == ["others"]
    assert others.collapsed == []                              # it is the page there
    body = html(client, "/enquiries?view=open")
    assert re.search(r'data-view="others"[^>]*>Other offices<b>6</b>', body)
    assert re.search(r'data-view="open"[^>]*>Open<b>1</b>', body)


def test_grouped_by_status_the_other_offices_come_last_and_collapsed(world):
    client, path, ids = world
    p = enquiries_client.get(client, "/enquiries?view=all&group=status")
    assert [k for k, _ in p.groups] == ["New", "others"]
    assert dict(p.groups)["New"] == ["OPP-1701"]
    assert p.collapsed == ["others"]
    body = html(client, "/enquiries?view=all")
    assert '"others_title": "Handled by other offices"' in body
    assert '"others": ""' in body and "not ours to chase" not in body


def test_group_by_office(world):
    client, path, ids = world
    p = enquiries_client.get(client, "/enquiries?view=all&group=office")
    assert [k for k, _ in p.groups] == ["JJC", "JJV", "JQM"]
    assert 'data-group="office"' in html(client, "/enquiries?view=all")


# ------------------------------------------------------------------ rules --
def test_the_chasing_rules_leave_other_offices_out_and_r8_keeps_them(world):
    client, path, ids = world
    conn = db.open_db(path)
    theirs = {ids[k] for k in ("jjv_new", "jjv_quoted", "jjv_won", "jqm_vendor", "jqm_answered", "jjv_lane")}
    sections = {s.rule: s for s in tracking.run(conn, TODAY)}
    for rule in ("R1", "R2", "R3", "R6", "R9", "R10"):
        assert not {i.enquiry_id for i in sections[rule].items} & theirs, rule
    assert ids["jjv_lane"] in {i.enquiry_id for i in sections["R8"].items}
    assert ids["jjc_new"] in {i.enquiry_id for i in sections["R1"].items}
    # without the field, the same cases are chased
    conn.execute("UPDATE enquiry SET handled_by = NULL")
    sections = {s.rule: s for s in tracking.run(conn, TODAY)}
    assert ids["jjv_new"] in {i.enquiry_id for i in sections["R1"].items}
    assert ids["jqm_vendor"] in {i.enquiry_id for i in sections["R2"].items}
    assert ids["jqm_answered"] in {i.enquiry_id for i in sections["R9"].items}
    assert ids["jjv_won"] in {i.enquiry_id for i in sections["R6"].items}
    conn.rollback()
    conn.close()


def test_the_new_case_invariant_covers_jjc_only(world):
    client, path, ids = world
    conn = db.open_db(path)
    assert [r["opp_no"] for r in tracking.new_cases_off_every_card(conn, TODAY)] == []
    conn.close()


def test_the_case_page_next_tile_says_handled(world):
    client, path, ids = world
    conn = db.open_db(path)
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids["jjv_new"],)).fetchone()
    assert tracking.case_nudge(conn, row, TODAY)["text"] == "Handled by JJV"
    conn.close()


def test_a_quoted_case_of_another_office_has_no_ladder_to_climb(world):
    client, path, ids = world
    body = html(client, "/enquiries/{}".format(ids["jjv_quoted"]))
    rail = body[body.index('class="n-title'):][:300]
    assert "Handled by JJV" in rail and "Decide" not in rail and " due" not in rail[:40]


# test_status_suggestions_still_apply_and_carry_the_steel_label: replaced in batch T1 by tests/test_tracking_order.py (it read the Tracking suggestions card).


# -------------------------------------------------- dashboard and report --
# test_the_dashboard_counts_jjc_only_and_adds_the_line: replaced in batch 18 by tests/test_inquiry_words.py (it read the old words).


# test_the_monthly_report_counts_jjc_only_and_adds_the_row: replaced in batch 18 by tests/test_inquiry_words.py (it read the old words).


# ----------------------------------------------------------------- export --
def test_the_export_carries_no_handled_by(world, tmp_path):
    client, path, ids = world
    from opplist import export_xlsx
    conn = db.open_db(path)
    written = export_xlsx.export(conn, tmp_path / "out" / "export.xlsx", db_path=str(path))
    from openpyxl import load_workbook
    ws = load_workbook(written, read_only=True)["Opportunities"]
    header = [c.value for c in next(ws.iter_rows(max_row=1))]
    assert header == export_xlsx.OPPORTUNITY_COLUMNS           # the 41, as they were
    conn.close()
