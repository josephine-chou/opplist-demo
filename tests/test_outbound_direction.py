"""Which way a mail of ours goes: asking a vendor (the 'log who we asked'
panel), answering the requester (no panel, Quoted on the sent date), or
neither. Our quote reply to KLX is the fixture (an invented mail with the
shape of an archived one)."""

from __future__ import annotations

import json
import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, intake, paths
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app
from test_outlook import out_mail  # noqa: F401  (helper)

QUOTE_REPLY = "reply_quote_to_requester.json"
PARTNER_QUESTION = "question_to_partner_over_intro_mail.json"
CY_DOOR_REQUEST = "rate_request_cy_to_door.json"


@pytest.fixture
def conn_with_parties(repo):
    """The parties these tests read the mails against (a copy of the one in
    test_outlook, on invented domains)."""
    seed_reference(repo)
    tolvex = add_party(repo, "Tolvex", country="Chile")
    klx = add_party(repo, "KLX", country="USA", full_name="KLX Logistics LLC")
    add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    add_party(repo, "Unknown", ptype="Unknown", country=None, placeholder=1)
    repo.insert("party_domain", {"domain": "andes-demo.cl", "party_id": tolvex})
    repo.insert("party_domain", {"domain": "shipklx.example", "party_id": klx})
    repo.commit()
    return repo.conn, {"tolvex": tolvex, "klx": klx, "tbc": tbc}


def _mail(name) -> MailInfo:
    with open(paths.FIXTURES_DIR / name, encoding="utf-8") as handle:
        data = json.load(handle)
    return MailInfo(
        subject=data["subject"], sender_name=data["sender_name"],
        sender_address=data["sender_address"], sender_internal=data["sender_internal"],
        received_on=data["received_on"], received_at=data["received_at"],
        to_addresses=data["to_addresses"], cc_addresses=data["cc_addresses"],
        display_to=data["display_to"], conversation_id=data["conversation_id"],
        entry_id=data["entry_id"], body_excerpt=data["body"])


def klx_reply() -> MailInfo:
    """Eric's quote to KLX, their request quoted below it."""
    return _mail(QUOTE_REPLY)


def partner_question() -> MailInfo:
    """Victor asking KDR (two domains, one company) whether they can take
    on a dangerous-goods job, quoted over KDR's own introduction mail."""
    return _mail(PARTNER_QUESTION)


def test_asking_whether_they_can_handle_it_is_asking():
    assert intake.mail_direction("RE: Warehouse support",
                                 "Will you be able to offer cross-docking at that site?") == "asking"
    assert intake.mail_direction("RE: Warehouse support", "Can you offer this?") == "asking"
    assert intake.mail_direction("RE: Warehouse support", "Do you handle lithium batteries in Reno?") == "asking"
    assert intake.mail_direction("RE: Warehouse support", "Could you be able to provide that?") == "asking"
    assert intake.mail_direction(
        "RE: Warehouse support",
        "The drums need a pick up in Boise and delivery to your warehouse "
        "before they are relabelled.") == "asking"
    assert intake.mail_direction("RE: Warehouse support", "Please advise your rates.") == "asking"
    # a sales visit, an introduction, a hello: nobody is asking for a rate
    assert intake.mail_direction(
        "Warehouse support",
        "Lena here from KDR - our Savannah team would be happy to meet yours "
        "whenever it suits.") == ""
    assert intake.mail_direction("RE: Warehouse support", "Cargo will route through Dallas. Thanks again.") == ""


def test_a_quoted_sales_visit_is_not_an_enquiry():
    from opplist.parsing import QuotedMail
    assert intake.quoted_asks_us(QuotedMail(
        address="lpardo@kdr-usa.example", subject="Warehouse support",
        text="Nils and I would be glad to hear what your team moves through the Southeast.\n")) is False
    assert intake.quoted_asks_us(QuotedMail(
        address="Gnovak@shipklx.example", subject="RATE REQUEST // DFW - GYE",
        text="Please quote DFW - GYE 4 drums.\n")) is True
    assert intake.quoted_asks_us(QuotedMail(
        address="ana@andes-demo.cl", subject="Plainfield IL",
        text="Could you send your best rates for 4 drums?\n")) is True
    assert intake.quoted_asks_us(QuotedMail(
        address="rmarino@seavio.example", subject="RE: RATE REQUEST // DFW - GYE",
        text="Our rates as follows:\nO/F USD 1,480 per 40HQ\n")) is False


def test_our_question_to_the_partner_asks_a_vendor_not_the_requester(conn_with_parties):
    conn, ids = conn_with_parties
    draft = intake.build(conn, partner_question())
    assert draft.direction == "asking"
    assert draft.outbound is True and draft.reply_to_requester is False
    assert [v["domain"] for v in draft.vendors] == ["kdr-usa.example", "kdrflorida.example"]
    assert all(v["checked"] for v in draft.vendors)          # the introduction is no enquiry
    assert all(v["note"] == "" for v in draft.vendors)
    assert [v["key"] for v in draft.vendors] == ["KDR", "KDR"]   # one company, two domains
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert draft.requester_key == "(TBC)"
    assert draft.values["status"] == "New"
    assert draft.values["received_on"] == "2026-09-16"      # what asked_on is taken from
    assert draft.vendor_note == ""
    assert not any("Reply to the requester" in note for note in draft.notes)


def test_the_final_destination_in_the_text_fills_the_destination(conn_with_parties):
    conn, ids = conn_with_parties
    conn.execute("INSERT INTO location (key, country, type, official_name) VALUES ('SSZ', 'Brazil', 'City', 'Santos')")
    draft = intake.build(conn, partner_question())
    # 'Should you need it, the final destination is Santos, Brazil.'
    assert draft.values["dest_loc"] == "SSZ" and draft.values["dest_country"] == "Brazil"
    assert draft.sources["dest_loc"] == intake.FROM_FINAL_DEST
    assert "destination not stated in this mail" not in draft.notes
    # the place is not on file: the country alone, and a note saying so
    mail = out_mail("Dangerous goods", ["owen@kdr-usa.example"],
                    body="Hi,\n\nCan you handle this? Final destination: Itajai, Brazil.\n")
    draft = intake.build(conn, mail)
    assert "dest_loc" not in draft.values and draft.values["dest_country"] == "Brazil"
    assert any("Itajai" in note for note in draft.notes)
    mail = out_mail("Dangerous goods", ["owen@kdr-usa.example"],
                    body="Hola,\n\nEl destino final es Temuco, Chile.\n")
    draft = intake.build(conn, mail)
    assert draft.values["dest_country"] == "Chile"


def test_a_reply_that_asks_the_writer_of_a_real_enquiry_keeps_them_as_requester(conn_with_parties):
    conn, ids = conn_with_parties
    mail = out_mail("RE: RATE REQUEST // DFW - GYE", ["Gnovak@shipklx.example"],
                    body="Hi Grant,\n\nCan you provide the dims?\n\n"
                         "From: Grant Novak <Gnovak@shipklx.example>\nSent: Monday\n"
                         "Subject: RATE REQUEST // DFW - GYE\n\nPlease quote DFW - GYE 4 drums.")
    draft = intake.build(conn, mail)
    assert draft.reply_to_requester is True and draft.vendors == []
    assert draft.values["requester_party_id"] == ids["klx"]
    assert draft.values["status"] == "New"


def test_the_case_requester_is_excluded_even_when_their_quoted_mail_asked_nothing(conn_with_parties):
    conn, ids = conn_with_parties
    mail = out_mail("RE: Warehouse support", ["lpardo@kdr-usa.example", "Gnovak@shipklx.example"],
                    body="Hi,\n\nWill you be able to offer cross-docking at that site?\n\n"
                         "From: Grant Novak <Gnovak@shipklx.example>\nSent: Monday\n"
                         "Subject: Warehouse support\n\nGood to see you last week.")
    draft = intake.build(conn, mail, case_requester_domains={"shipklx.example"})
    assert [v["domain"] for v in draft.vendors] == ["kdr-usa.example"]
    assert draft.values["requester_party_id"] == ids["klx"]


def test_a_short_initialism_drops_its_region_and_keeps_its_capitals():
    assert intake.suggested_party_name("kdr-usa.example") == "KDR"
    assert intake.suggested_party_name("kdrflorida.example") == "KDR"
    assert intake.suggested_party_name("dhl-mexico.example") == "DHL"
    assert intake.suggested_party_name("klx.example") == "KLX"
    assert intake.suggested_party_name("cargovia-qatar.example") == "Cargovia Qatar"   # the country tells branches apart
    assert intake.suggested_party_name("focus.example") == "Focus"                         # not 'foc' + us
    assert intake.suggested_party_name("us.yangming.example") == "Yangming"
    assert intake.suggested_party_key("cosco-usa.example") == "Cosco-Usa"                   # import-tracker, unchanged


def test_the_direction_of_our_own_words():
    assert intake.mail_direction("RATE REQUEST // DFW - GYE", "Hi,\n\nPlease advise your rates.") == "asking"
    assert intake.mail_direction("Re: Plainfield IL", "Please quote your best rates for 4 drums") == "asking"
    assert intake.mail_direction("Re: Wheeling to Shannon",
                                 "Hello Tobias,\n\nAttached is our quotation for the air option, "
                                 "with the LCL option on a second sheet.") == "quoting"
    assert intake.mail_direction("RE: RATE REQUEST // IAH - CLO",
                                 "Hi Ana,\n\nOur rates as follows:\nO/F USD 1,480 per 40HQ\nTHC USD 275") == "quoting"
    assert intake.mail_direction("RE: shipment", "Charges: USD 2,640 all-in door to door.") == "quoting"
    assert intake.mail_direction("RE: shipment", "Target rate USD 1700 - please quote your best.") == "asking"
    assert intake.mail_direction("ORCHARD FTL // 10/05", "Two trucks needed for next week, please set them up.") == ""
    assert intake.mail_direction("RE: Wheeling", "The destination charges are still outstanding; "
                                                          "a quote follows once they arrive.") == ""


def test_our_reply_to_klx_is_a_quote_not_a_vendor_request(conn_with_parties):
    conn, ids = conn_with_parties
    draft = intake.build(conn, klx_reply())
    assert draft.direction == "quoting"
    assert draft.reply_to_requester is True and draft.outbound is False
    assert draft.vendors == []                                   # no panel
    assert draft.values["requester_party_id"] == ids["klx"]
    assert draft.requester_key == "KLX"
    assert draft.values["status"] == "Quoted"
    assert draft.values["status_at"] == "2026-09-15"            # the day it went out
    assert draft.values["received_on"] == "2026-09-14"          # the day KLX asked
    assert draft.vendor_note == "Reply to the requester (KLX), not a vendor request"


def test_asking_a_vendor_with_the_requester_copied_in(conn_with_parties):
    conn, ids = conn_with_parties
    mail = out_mail("RE: RATE REQUEST // DFW - GYE",
                    ["rmarino@seavio.example", "Gnovak@shipklx.example"],
                    body="Hi Remo,\n\nPlease quote your best rates for the cargo below.\n\n"
                         "From: Grant Novak <Gnovak@shipklx.example>\nSent: Monday\n"
                         "Subject: RATE REQUEST // DFW - GYE\n\nPlease quote DFW - GYE 4 drums.")
    draft = intake.build(conn, mail)
    assert draft.direction == "asking" and draft.outbound is True
    assert [v["domain"] for v in draft.vendors] == ["seavio.example"]   # KLX is not a vendor
    assert draft.values["requester_party_id"] == ids["klx"]
    assert draft.values["status"] == "New"
    assert draft.vendor_note == "Reply to the requester (KLX), not a vendor request"


def test_the_case_requester_is_never_a_vendor(conn_with_parties):
    conn, ids = conn_with_parties
    mail = out_mail("RATE REQUEST // DFW - GYE", ["rmarino@seavio.example", "Gnovak@shipklx.example"])
    draft = intake.build(conn, mail, case_requester_domains={"shipklx.example"})
    assert [v["domain"] for v in draft.vendors] == ["seavio.example"]
    assert draft.vendor_note.startswith("Reply to the requester (KLX)")
    # only the requester on it: an answer, whatever the words
    mail = out_mail("RATE REQUEST // DFW - GYE", ["Gnovak@shipklx.example"])
    draft = intake.build(conn, mail, case_requester_domains={"shipklx.example"})
    assert draft.vendors == [] and draft.reply_to_requester is True


def test_a_mail_that_neither_asks_nor_quotes_offers_nobody(conn_with_parties):
    conn, ids = conn_with_parties
    mail = out_mail("ORCHARD FTL // 10/05 AND 10/06 // ROCKFORD, IL - FRESNO, CA",
                    ["Gnovak@shipklx.example"],
                    body="Hi Grant,\n\nWe will need two trucks on those dates, details below. Please set them up.\n")
    draft = intake.build(conn, mail)
    assert draft.direction == "" and draft.outbound is True
    assert draft.vendors == []
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert any("nothing in it asks for a rate" in note for note in draft.notes)


def test_our_quote_to_somebody_new_is_quoted_on_the_sent_date(conn_with_parties):
    conn, ids = conn_with_parties
    mail = out_mail("RE: Rate Quote - Mayaguez, PR", ["buyer@newcustomer.example"],
                    body="Hi,\n\nPlease find our quote attached.\n\n"
                         "From: Buyer <buyer@newcustomer.example>\nSent: Monday\n"
                         "Subject: Rate Quote - Mayaguez, PR\n\nCan you quote Mayaguez?")
    draft = intake.build(conn, mail)
    assert draft.reply_to_requester is True and draft.vendors == []
    assert draft.values["status"] == "Quoted" and draft.values["status_at"] == "2026-09-04"
    assert draft.new_party and draft.new_party["domain"] == "newcustomer.example"


@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "web.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    klx = add_party(repo, "KLX", country="USA")
    repo.insert("party_domain", {"domain": "shipklx.example", "party_id": klx})
    add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    add_enquiry(repo, "OPP-1700", klx, received="2026-09-14", status="New",
                origin_loc="MIA", dest_loc="SAI", service_mode="Air")
    db.set_setting(conn, "next_opp_number", 1701)
    repo.commit()
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    holder = {"mail": None}
    from opplist import outlook_bridge
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: holder["mail"])
    return app.test_client(), path, holder


def test_register_from_the_klx_reply_shows_no_panel_and_quotes_on_the_sent_date(web):
    client, path, holder = web
    holder["mail"] = klx_reply()
    page = client.get("/enquiries/from-outlook")
    body = page.data.decode("utf-8")
    assert "We sent this mail out - log who we asked" not in body
    assert 'name="status_at" value="2026-09-15"' in body
    assert "dated the day the mail went out: 2026-09-15" in body
    assert "Reply to the requester (KLX), not a vendor request" in body

    page = client.post("/enquiries/new", data={
        "created_by": "outlook", "received_on": "2026-09-14", "status": "Quoted",
        "status_at": "2026-09-15", "requester_party_id": "1", "service_mode": "Air",
        "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile",
        "dest_loc": "SAI", "commodity": "Pump housings", "air_weight_kg": "410",
        "decision": "new"},
        follow_redirects=True)
    assert page.status_code == 200
    conn = db.open_db(path)
    row = conn.execute("SELECT id, status FROM enquiry WHERE opp_no = 'OPP-1701'").fetchone()
    assert row["status"] == "Quoted"
    assert [(r["at"], r["status"]) for r in conn.execute(
        "SELECT at, status FROM status_event WHERE enquiry_id = ? ORDER BY id", (row["id"],))] == \
        [("2026-09-14", "New"), ("2026-09-15", "Quoted")]
    conn.close()


def test_log_vendor_from_outlook_never_logs_the_case_requester(web):
    client, path, holder = web
    holder["mail"] = out_mail("RATE REQUEST // DFW - GYE",
                              ["rmarino@seavio.example", "Gnovak@shipklx.example"])
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Logged 1 vendor request: Seavio" in body
    assert "Reply to the requester (KLX), not a vendor request" in body
    holder["mail"] = klx_reply()
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    assert "Reply to the requester (KLX), not a vendor request - nothing was logged." in page.data.decode("utf-8")
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM vendor_request").fetchone()[0] == 1
    conn.close()


# test_register_from_the_bqr_question_offers_both_bqr_domains_as_one_vendor: replaced in 15c by tests/test_register_v3.py (the register page is the case page's cards).


def test_log_vendor_from_outlook_logs_the_partner_once_for_its_two_domains(web):
    client, path, holder = web
    holder["mail"] = partner_question()
    page = client.post("/enquiries/1/vendors/from-outlook", follow_redirects=True)
    body = page.data.decode("utf-8")
    assert "Logged 1 vendor request: KDR" in body
    assert "already on the case" not in body
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM vendor_request").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM party WHERE lower(key) = 'kdr'").fetchone()[0] == 1
    conn.close()


# ---------------------------------- 'please quote below' is a request (Orbis)
def orbis_request() -> MailInfo:
    from opplist import outlook_bridge
    return outlook_bridge.read_fixture(paths.FIXTURES_DIR / CY_DOOR_REQUEST)


def test_please_quote_below_is_us_asking_not_giving():
    assert intake.mail_direction(
        "RATE REQUEST // MEMPHIS CY - SANTO DOMINGO DOOR // NON-STACKABLE SKIDS",
        "Hi Dana,\n\nPlease quote below, door delivery from the CY:\n\n"
        "Need 40' rate since the skids cannot be stacked.") == "asking"
    # the imperative alone, whatever the subject
    assert intake.mail_direction("Re: Santo Domingo", "Please quote below, door delivery from the CY.") == "asking"
    # 'Need 40' rate': the loosened need clause
    assert intake._ASK_BODY.search("Need 40' rate since the skids cannot be stacked")
    # a rate given stays a rate given
    assert intake.mail_direction("Re: Santo Domingo", "Please find our quote below.") == "quoting"
    assert intake.mail_direction("Re: Santo Domingo", "Our rates are as below: USD 1,390/40'") == "quoting"
    assert intake.mail_direction("Re: Santo Domingo", "Rates are attached.") == "quoting"
    assert intake.mail_direction("Re: Santo Domingo", "As quoted below, all in.") == "quoting"
    # an imperative with money under it is a quote with a question, not a request
    assert intake.mail_direction("RATE REQUEST // MEM - SDQ",
                                 "Please quote below.\nUSD 2,860 all-in door to door.") == "quoting"


def test_the_orbis_rate_request_offers_orbis_as_a_vendor(conn_with_parties):
    conn, ids = conn_with_parties
    draft = intake.build(conn, orbis_request())
    assert draft.direction == "asking"
    assert draft.outbound is True and draft.reply_to_requester is False
    assert [(v["domain"], v["checked"]) for v in draft.vendors] == [("orbisworldwide.example", True)]
    assert draft.vendors[0]["key"] == "Orbisworldwide"
    assert draft.values["requester_party_id"] == ids["tbc"]
    assert draft.values["status"] == "New"
    assert draft.values["origin_loc"] == "MEMPHIS CY" and draft.values["dest_loc"] == "SANTO DOMINGO"


def test_the_partner_and_klx_fixtures_still_read_the_same(conn_with_parties):
    conn, ids = conn_with_parties
    assert intake.build(conn, partner_question()).direction == "asking"
    klx = intake.build(conn, klx_reply())
    assert klx.direction == "quoting" and klx.reply_to_requester is True


def test_the_selftest_reads_a_fixture(tmp_path, capsys):
    from opplist.__main__ import main

    path = tmp_path / "self.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    repo.commit()
    conn.close()
    assert main(["outlook-selftest", "--db", str(path),
                 "--fixture", str(paths.FIXTURES_DIR / CY_DOOR_REQUEST)]) == 0
    out = capsys.readouterr().out
    assert "direction      : asking" in out and re.search(r"status\s+New\s", out) and "Sourcing" not in out
    assert "[x] orbisworldwide.example" in out and "Orbisworldwide  (new party)" in out
