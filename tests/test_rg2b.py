"""Batch RG2b: the Owner starts as the JJC colleague whose mail she registers
from; Who asked? reads a mail she picks in Outlook and knows the JoJo
offices; a new party starts from the MFN directory (WC2); the reader's
monthly check and its table on Settings.

Every company, person, address, place, number and directory row here is
invented (.test domains); cases from OPP-9451."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from conftest import add_enquiry, add_party
from opplist import db, intake, outlook_bridge, paths, reader_check, reader_month, mfn_link, who_asked
from opplist.web import create_app
from test_rg2a import INQUIRY, master_world, register_form

OFFICES_CSV = ('"member_id","company","formerly_known_as","office_type","office_city","city","country",'
               '"country_code","web_domain","email_domain"\n'
               '"90417","Quayside Cargo Ltda","","Head Office","Port Alder","Port Alder","Chile","CL",'
               '"quayside.test","quayside.test"\n'
               '"90002","Tidewell Shipping Brasil","","Branch Office","Quillmouth","Quillmouth","Brazil","BR",'
               '"tidewell.test","tidewell.test"\n'
               '"90003","Tidewell Shipping USA","","Head Office","Bransford","Bransford","USA","US",'
               '"tidewell.test","tidewell.test"\n'
               '"90004","Larkspur Freight SpA","","Head Office","Santiago","Santiago","Chile","CL",'
               '"larkspur.test","larkspur.test"\n'
               '"90005","Harrow Global Chile","","Branch Office","Santiago","Santiago","Chile","CL",'
               '"harrowglobal.test","harrowglobal.test"\n'
               '"90006","Harrow Global Brasil","","Branch Office","Quillmouth","Quillmouth","Brazil","BR",'
               '"harrowglobal.test","harrowglobal.test"\n')


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "rg2b.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    ids = master_world(repo, tmp_path)
    db.set_setting(conn, "owners", ["Victor", "Mara", "Odile"])
    db.set_setting(conn, intake.ME_SETTING, "Odile")                  # her, on this invented desk
    ids["tbc"] = conn.execute("SELECT id FROM party WHERE key = '(TBC)'").fetchone()[0]
    ids["szx"] = add_party(repo, "JoJo JJZ", ptype="Office", country="Unknown")
    repo.commit()
    folder = paths.DATA_DIR / "mfn" / "directory"                      # conftest's scratch data folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mfn-offices.csv").write_text(OFFICES_CSV, encoding="utf-8")
    mfn_link._CACHE.clear()
    holder = {"mail": None}

    def selection():
        if isinstance(holder["mail"], Exception):
            raise holder["mail"]
        return holder["mail"]
    monkeypatch.setattr(outlook_bridge, "read_selection", selection)
    monkeypatch.setattr(outlook_bridge, "read_attachments", lambda entry_id: [])
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids, "holder": holder}
    conn.close()
    mfn_link._CACHE.clear()


def client_of(world):
    world["conn"].commit()
    app = create_app(world["path"])
    app.config["TESTING"] = True
    return app.test_client()


def mail(subject, body="", sender="tess@larkspur.test", name="Tess Wren", internal=False, key="9451",
         to=("desk@jojofreight.test",), to_names=("JJC-DESK",)):
    return outlook_bridge.MailInfo(subject=subject, received_on="2027-03-04", received_at="2027-03-04 09:40",
                                   sender_address=sender, sender_name=name, sender_internal=internal,
                                   to_addresses=list(to), to_names=list(to_names), body_excerpt=body,
                                   entry_id="ENTRY-" + key, conversation_id="CONV-" + key)


def forward(name="JJC-VICTOR LANG", sender="victor@jojofreight.test", key="9451"):
    return mail("FW: Rate request", "Please register this one.\n\n" + INQUIRY, sender=sender, name=name,
                internal=True, key=key)


# ------------------------------------------------------------ 1. owner --
def test_a_colleagues_forward_starts_the_owner_as_that_colleague(world):
    draft = intake.build(world["conn"], forward())
    assert draft.values["owner"] == "Victor"
    assert draft.sources["owner"] == "the colleague whose mail this is (JJC-VICTOR LANG)"


def test_a_colleagues_mail_asking_vendors_quoting_the_inquiry_starts_the_owner(world):
    asking = mail("RE: Rate request", "Dear partner,\nPlease quote below.\nVictor\n\n" + INQUIRY,
                  sender="victor@jojofreight.test", name="JJC-VICTOR LANG", internal=True,
                  to=("rates@orvane.test",), to_names=("Orvane Rates",))
    assert intake.build(world["conn"], asking).values.get("owner") == "Victor"


def test_two_owners_matching_or_none_leave_the_owner_empty(world):
    conn = world["conn"]
    db.set_setting(conn, "owners", ["Victor", "Victor Lang", "Mara"])
    assert "owner" not in intake.build(conn, forward()).values
    db.set_setting(conn, "owners", ["Mara"])
    assert "owner" not in intake.build(conn, forward()).values


def test_her_own_mail_and_an_outside_mail_leave_the_owner_empty(world):
    conn = world["conn"]
    assert "owner" not in intake.build(conn, forward(name="JJC-ODILE MARSH", sender="odile@jojofreight.test")).values
    assert "owner" not in intake.build(conn, mail("Rate request", INQUIRY)).values
    # another office's forward is that office's mail, not a JJC colleague's
    assert "owner" not in intake.build(conn, forward(name="JJZ-VICTOR LANG", sender="v.lau@jojofreight.test")).values


def test_the_register_form_shows_the_owner_and_saves_what_she_leaves(world):
    world["holder"]["mail"] = forward()
    client = client_of(world)
    page = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert '<option value="Victor" selected>Victor</option>' in page
    assert "the colleague whose mail this is (JJC-VICTOR LANG)" in page
    conn = world["conn"]
    draft = intake.build(conn, forward())
    saved = client.post("/enquiries/new", data=register_form(conn, draft, owner="Mara", dest_loc="PORT ALDER",
                                                              dest_country="Chile"))
    assert saved.status_code == 302
    assert conn.execute("SELECT owner FROM enquiry ORDER BY id DESC").fetchone()[0] == "Mara"   # her change
    draft = intake.build(conn, forward(key="9452"))
    client.post("/enquiries/new", data=register_form(conn, draft, owner="Nobody Here", dest_loc="PORT ALDER",
                                                     dest_country="Chile", source_subject="FW: Rate request 2",
                                                     decision="new"))
    assert conn.execute("SELECT owner FROM enquiry ORDER BY id DESC").fetchone()[0] is None   # not an owner


# ------------------------------------------------- 2. From Outlook --
@pytest.fixture
def tbc_case(world):
    case = add_enquiry(world["repo"], "OPP-9451", world["ids"]["tbc"], received="2027-03-03",
                       source_subject="Rate request", origin_country="USA", dest_country="Chile")
    world["repo"].commit()
    return case


def page_of(client, case):
    return client.get("/enquiries/{}".format(case)).data.decode("utf-8")


def requester_of(world, case):
    return world["conn"].execute("SELECT requester_party_id FROM enquiry WHERE id = ?", (case,)).fetchone()[0]


def test_from_outlook_offers_a_known_party_and_writes_nothing_before_the_click(world, tbc_case):
    client = client_of(world)
    assert 'id="who-from-outlook"' in page_of(client, tbc_case)
    world["holder"]["mail"] = mail("Rate request", INQUIRY)
    changes = world["conn"].execute("SELECT count(*) FROM change_log").fetchone()[0]
    client.post("/enquiries/{}/who-asked/from-outlook".format(tbc_case))
    page = page_of(client, tbc_case)
    assert "Use Larkspur Freight as requester" in page and "and attach this thread to the case" in page
    assert requester_of(world, tbc_case) == world["ids"]["tbc"]
    assert world["conn"].execute("SELECT count(*) FROM change_log").fetchone()[0] == changes


def test_use_from_outlook_sets_the_requester_and_attaches_the_thread(world, tbc_case):
    client = client_of(world)
    world["holder"]["mail"] = mail("Rate request", INQUIRY, key="9461")
    client.post("/enquiries/{}/who-asked/from-outlook".format(tbc_case))
    done = client.post("/enquiries/{}/who-asked/use".format(tbc_case), data={
        "party_id": world["ids"]["larkspur"], "evidence": "From Outlook", "attach": "1",
        "conversation_id": "CONV-9461", "subject": "Rate request"}, follow_redirects=True).data.decode("utf-8")
    assert requester_of(world, tbc_case) == world["ids"]["larkspur"]
    assert "the requester is now Larkspur Freight. The thread is attached." in done
    kinds = {r[0]: r[1] for r in world["conn"].execute(
        "SELECT kind, value FROM enquiry_thread WHERE enquiry_id = ?", (tbc_case,))}
    assert kinds.get("conversation") == "CONV-9461" and kinds.get("thread_key")


def test_from_outlook_offers_an_unknown_address_with_add_party(world, tbc_case):
    client = client_of(world)
    world["holder"]["mail"] = mail("Rate request", "Hello,\nPlease quote 2 pallets to Port Alder.\nNed\n",
                                   sender="ned@quillfield.test", name="Ned Pike")
    client.post("/enquiries/{}/who-asked/from-outlook".format(tbc_case))
    page = page_of(client, tbc_case)
    assert "ned@quillfield.test" in page and "Add party" in page and "from Outlook" in page
    assert requester_of(world, tbc_case) == world["ids"]["tbc"]


def test_from_outlook_offers_a_own_office(world, tbc_case):
    client = client_of(world)
    world["holder"]["mail"] = mail("Rate request", "Hi JJC,\nPlease quote 1x40HQ to Port Alder.\nAna\n",
                                   sender="ana.li@jojofreight.test", name="JJZ-ANA LI", internal=True)
    client.post("/enquiries/{}/who-asked/from-outlook".format(tbc_case))
    assert "Use JoJo JJZ as requester" in page_of(client, tbc_case)


def test_outlook_closed_or_nothing_selected_is_one_line(world, tbc_case):
    client = client_of(world)
    world["holder"]["mail"] = outlook_bridge.OutlookError("Outlook is not open. Open it and select a mail.")
    page = client.post("/enquiries/{}/who-asked/from-outlook".format(tbc_case),
                       follow_redirects=True).data.decode("utf-8")
    assert "Outlook is not open. Open it and select a mail." in page
    assert requester_of(world, tbc_case) == world["ids"]["tbc"]


# ------------------------------------------------------- 3. offices --
def test_the_case_s_own_mails_offer_the_office_party(world, tbc_case):
    conn = world["conn"]
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (tbc_case,)).fetchone()
    from_office = mail("Rate request", "Please quote.", sender="ana.li@jojofreight.test", name="JJZ-ANA LI",
                       internal=True)
    from_kul = mail("Rate request", "Please quote.", sender="bo@jojofreight.test", name="JJK-BO TAN", internal=True)
    found = who_asked.from_mails(conn, row, [from_office, from_kul])
    assert [(c.party_key, c.suggested_key) for c in found] == [("JoJo JJZ", ""), ("", "JoJo JJK")]
    client = client_of(world)
    client.post("/enquiries/{}/who-asked/add-party".format(tbc_case), data={"office": "JJK", "key": "x"})
    party = conn.execute("SELECT type FROM party WHERE key = 'JoJo JJK'").fetchone()
    assert party is not None and party[0] == "Office"


# ---------------------------------------------------------- 4. WC2 --
def test_one_mfn_office_fills_the_new_party(world):
    conn = world["conn"]
    # FX7: one office fills when the signature names its city or country
    draft = intake.build(conn, mail("Rate request", "Hello,\nPlease quote 2 pallets to Port Alder.\nBest regards,\nNed Pike\nQuayside Cargo Ltda\nPort Alder, Chile\n",
                                    sender="ned@quayside.test", name="Ned Pike"))
    new = draft.new_party
    assert (new["key"], new["country"], new["city"], new["mfn_no"], new["domain"]) == \
        ("Quayside Cargo Ltda", "Chile", "Port Alder", "90417", "quayside.test")
    assert new["sources"]["key"] == intake.MFN_LINE and new["mfn"].startswith("from the MFN directory")


def test_the_party_is_made_at_register_with_the_directory_s_fields(world):
    conn = world["conn"]
    sent = mail("Rate request", "Hello,\nPlease quote 2 pallets to Port Alder.\nBest regards,\nNed Pike\nQuayside Cargo Ltda\nPort Alder, Chile\n",
                sender="ned@quayside.test", name="Ned Pike", key="9471")
    world["holder"]["mail"] = sent
    client = client_of(world)
    parties = conn.execute("SELECT count(*) FROM party").fetchone()[0]
    page = client.post("/enquiries/from-outlook").data.decode("utf-8")
    assert "from the MFN directory (90417 Quayside Cargo Ltda, Port Alder)" in page and 'value="90417"' in page
    assert conn.execute("SELECT count(*) FROM party").fetchone()[0] == parties        # only read
    draft = intake.build(conn, sent)
    client.post("/enquiries/new", data=register_form(
        conn, draft, requester_party_id="", new_party_key="Quayside Cargo Ltda", new_party_type="Agent",
        new_party_country="Chile", new_party_domain="quayside.test", new_party_domain_shown="1",
        new_party_city="Port Alder", new_party_mfn_no="90417", dest_loc="PORT ALDER", dest_country="Chile"))
    row = conn.execute("SELECT city, mfn_no, mfn_member, country FROM party WHERE key = 'Quayside Cargo Ltda'").fetchone()
    assert tuple(row) == ("Port Alder", "90417", 1, "Chile")


def test_several_offices_abroad_are_listed_and_nothing_filled(world):
    draft = intake.build(world["conn"], mail("Rate request", "Hello,\nPlease quote to Port Alder.\nIvo\n",
                                             sender="ivo@tidewell.test", name="Ivo Sand"))
    new = draft.new_party
    assert [o["company"] for o in new["mfn_offices"]] == ["Tidewell Shipping Brasil", "Tidewell Shipping USA"]
    assert "mfn_no" not in new and new.get("sources", {}).get("key") != intake.MFN_LINE


def test_several_offices_the_one_in_the_mail_s_country(world):
    draft = SimpleNamespace(new_party={"key": "Harrowglobal", "type": "Agent", "country": "Chile",
                                       "domain": "harrowglobal.test", "sources": {}})
    intake.mfn_new_party(world["conn"], draft)
    assert (draft.new_party["key"], draft.new_party["mfn_no"]) == ("Harrow Global Chile", "90005")


def test_a_domain_on_file_is_not_prefilled(world):
    draft = intake.build(world["conn"], mail("Rate request", INQUIRY))
    assert draft.values["requester_party_id"] == world["ids"]["larkspur"] and not draft.new_party
    stub = SimpleNamespace(new_party={"key": "Larkspur", "type": "Agent", "country": "Chile",
                                      "domain": "larkspur.test", "sources": {}})
    intake.mfn_new_party(world["conn"], stub)
    assert "mfn_no" not in stub.new_party and stub.new_party["key"] == "Larkspur"


# ------------------------------------------------- 5. monthly check --
def _reads(world, rows, at="2026-12-05 10:00:00"):
    repo = world["repo"]
    case = add_enquiry(repo, "OPP-9481", world["ids"]["larkspur"], received="2026-12-04")
    for field, read, saved in rows:
        repo.insert("register_read", {"enquiry_id": case, "field": field, "value_read": read,
                                      "read_from": "mail", "value_saved": saved, "created_at": at})
    repo.commit()


def test_the_check_runs_on_the_first_once_for_each_month(world, tmp_path, monkeypatch):
    _reads(world, [("requester_party_id", "Larkspur Freight", "larkspur freight"),
                   ("dest_loc", "PORT ALDER", "SANTIAGO"), ("container_type", "40HQ", "40HQ"),
                   ("container_qty", "2", "2")], at="2026-09-01 08:00:00")
    seen = []
    monkeypatch.setattr(reader_check, "check", lambda *a, **k: pytest.fail("read the archive"))
    folder = tmp_path / "reports"
    assert reader_month.due(dt.date(2026, 9, 30), folder) == []                  # September not over
    lines = reader_month.run_due(world["conn"], dt.date(2026, 10, 1), folder)
    assert lines == ["reader check for 2026-09 (register_read): reader-month-2026-09.json"]
    kept = json.loads((folder / "reader-month-2026-09.json").read_text(encoding="utf-8"))
    assert kept["fields"]["requester"] == {"right": 1, "count": 1}
    assert kept["fields"]["dest_loc"] == {"right": 0, "count": 1}
    assert kept["fields"]["containers"] == {"right": 1, "count": 1}
    for day in (dt.date(2026, 10, 1), dt.date(2026, 10, 2), dt.date(2026, 10, 20)):
        assert reader_month.run_due(world["conn"], day, folder) == []          # once
    assert [l.split(" (")[0] for l in reader_month.run_due(world["conn"], dt.date(2026, 11, 3), folder)] == \
        ["reader check for 2026-10"]                                             # the first run after the 1st
    assert seen == []


def test_a_month_before_register_read_is_measured_on_the_archive(world, tmp_path, monkeypatch):
    _reads(world, [("requester_party_id", "Larkspur Freight", "Larkspur Freight")], at="2026-11-20 10:00:00")
    verdicts = {"OPP-9482": {f: [1, 1, "right"] for f in reader_check.FIELDS}}
    verdicts["OPP-9482"]["dest_loc"] = ["A", "B", "wrong"]
    verdicts["OPP-9483"] = {f: [1, None, "empty"] for f in reader_check.FIELDS}
    monkeypatch.setattr(reader_check, "check", lambda conn, search, months: {months[0]: verdicts})

    class Search:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    got = reader_month.measure(world["conn"], "2026-11", search_factory=Search)
    assert got["source"] == "reader-check"
    assert got["fields"]["dest_loc"] == {"right": 0, "count": 1}                 # an empty one gave nothing
    assert got["fields"]["origin_loc"] == {"right": 1, "count": 1}
    assert reader_month.measure(world["conn"], "2026-12")["source"] == "register_read"


def test_the_scheduled_check_reads_only_the_live_database(world, tmp_path):
    assert reader_month.scheduled_check(world["path"], dt.date(2026, 12, 1)) == []


def test_settings_shows_the_table(world, tmp_path, monkeypatch):
    folder = tmp_path / "reports"
    folder.mkdir()
    monkeypatch.setattr(reader_check, "REPORT_DIR", folder)
    for month, right in (("2026-09", 3), ("2026-10", 4)):
        (folder / "reader-month-{}.json".format(month)).write_text(json.dumps(
            {"month": month, "source": "reader-check",
             "fields": {"origin_loc": {"right": right, "count": 4}}}), encoding="utf-8")
    (folder / "reader-month-2026-08.json").write_text(json.dumps(
        {"fields": {"origin_loc": {"right": 1, "count": 1}}}), encoding="utf-8")       # before September
    body = client_of(world).get("/settings").data.decode("utf-8")
    assert "Register reader, month by month" in body and "2026-08" not in body
    assert '<th class="num">2026-09</th><th class="num">2026-10</th>' in body
    assert '75% <span class="m">(4)</span>' in body and '100% <span class="m">(4)</span>' in body
    assert reader_month.table(folder)["rows"] == [{"label": "Origin place", "cells": [
        {"share": 75, "count": 4}, {"share": 100, "count": 4}]}]
