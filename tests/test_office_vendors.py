"""Another of our offices asked for the buying rate is a vendor request
(OPP-1294, 2026-09-25). Their addresses are on our own domain, so the
office code in the recipients' display names ('JJH-DANIEL YIP') is what
says whom we asked; only the To line counts, Cc'd offices are noted."""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, intake, mail_archive, outlook_bridge, paths
from opplist.outlook_bridge import MailInfo, read_fixture
from opplist.web import create_app

FIXTURE = paths.FIXTURES_DIR / "office_colleagues_asked_for_rates.json"


def jjh_mail() -> MailInfo:
    return read_fixture(FIXTURE)


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "office.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    challenge = add_party(repo, "Summit Cargo", country="USA")
    orbis = add_party(repo, "Orbis Worldwide", country="USA")
    repo.insert("party_domain", {"domain": "orbisworldwide.example", "party_id": orbis})
    # the live file has JoJo JJH as party 74; here it is whatever id comes
    jjh = add_party(repo, "JoJo JJH", ptype="Office", country="USA")
    add_party(repo, "JoJo JJC", ptype="Office", country="USA")
    tbc = add_party(repo, "(TBC)", ptype="Unknown", country=None, placeholder=1)
    case = add_enquiry(repo, "OPP-1294", challenge, received="2026-08-18", service_mode="Air",
                       origin_country="China", origin_loc="NGB", dest_country="USA", dest_loc="MEM")
    repo.commit()
    return conn, path, {"jjh": jjh, "orbis": orbis, "tbc": tbc, "case": case, "challenge": challenge}


def test_the_fixture_reads_the_names(world):
    mail = jjh_mail()
    assert mail.to_names == ["JJH-DANIEL YIP", "JJH-GLORIA TAM", "JJH-KELVIN HO"]
    assert mail.cc_names == ["#JJC-MFN", "JQM-INES ARCE"]
    assert mail.sender_internal and mail.message_id == "<office-ask-0818@jojofreight.example>"
    assert intake._office_recipients(mail) == {"JJH": ["JJH-DANIEL YIP", "JJH-GLORIA TAM", "JJH-KELVIN HO"]}
    assert intake._office_copied(mail) == ["JQM"]                # the alias is nobody's office
    assert intake._office_names(["JJC-AVA TRAN", "#JJC-MFN", "jjv-mei sun", "", "Dana Morrow"]) == {}


def test_the_jjh_request_offers_the_office_as_the_one_vendor(world):
    conn, path, ids = world
    draft = intake.build(conn, jjh_mail())
    assert draft.direction == "asking" and draft.outbound
    assert [v["key"] for v in draft.vendors] == ["JoJo JJH"]
    vendor = draft.vendors[0]
    assert vendor["party_id"] == ids["jjh"] and vendor["known"] and vendor["checked"]
    assert vendor["domain"] == "office:JJH" and vendor["office"] == "JJH"
    assert vendor["addresses"] == ["JJH-DANIEL YIP", "JJH-GLORIA TAM", "JJH-KELVIN HO"]
    assert "JQM copied, not offered" in draft.notes
    # the requester side is untouched: parked on the placeholder, no office
    assert draft.values["requester_party_id"] == ids["tbc"] and draft.requester_key == "(TBC)"
    assert draft.sources["requester_party_id"] == intake.FROM_OUTBOUND
    assert draft.values["status"] == "New"


def test_a_mail_to_jjc_colleagues_only_offers_nobody(world):
    conn, path, ids = world
    mail = jjh_mail()
    mail.to_addresses = ["AvaT@jojofreight.example"]
    mail.to_names = ["JJC-AVA TRAN"]
    mail.cc_addresses, mail.cc_names = ["jjc-mfn@jojofreight.example"], ["#JJC-MFN"]
    mail.display_to = "JJC-AVA TRAN"
    draft = intake.build(conn, mail)
    assert draft.vendors == [] and not draft.outbound and draft.direction == ""
    assert "requester_party_id" not in draft.values


def test_an_outside_vendor_and_an_office_are_both_offered(world):
    conn, path, ids = world
    mail = jjh_mail()
    mail.to_addresses = ["dmorrow@orbisworldwide.example"] + list(mail.to_addresses)
    mail.to_names = ["Dana Morrow (Orbis Worldwide)"] + list(mail.to_names)
    draft = intake.build(conn, mail)
    assert [v["key"] for v in draft.vendors] == ["Orbis Worldwide", "JoJo JJH"]
    assert draft.vendors[0]["domain"] == "orbisworldwide.example" and draft.vendors[0]["party_id"] == ids["orbis"]
    assert draft.vendors[1]["domain"] == "office:JJH"


def test_an_office_not_on_file_keeps_its_key_and_says_so(world):
    conn, path, ids = world
    mail = jjh_mail()
    mail.to_names = ["JQB-ANAN P"]
    draft = intake.build(conn, mail)
    assert [(v["key"], v["party_id"], v["known"]) for v in draft.vendors] == [("JoJo JQB", None, False)]
    assert "not on file" in draft.vendors[0]["note"]


def _client(path, monkeypatch, mail):
    app = create_app(path)
    app.config["TESTING"] = True
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: mail)
    return app.test_client()


def test_the_vendors_route_logs_the_office_once(world, monkeypatch):
    conn, path, ids = world
    conn.close()
    client = _client(path, monkeypatch, jjh_mail())
    url = "/enquiries/{}/vendors/from-outlook".format(ids["case"])
    body = client.post(url, follow_redirects=True).data.decode("utf-8")
    assert "Logged 1 vendor request: JoJo JJH" in body
    conn = db.open_db(path)
    rows = conn.execute("SELECT vendor_party_id, asked_on, thread_key, conversation_id FROM vendor_request "
                        "WHERE enquiry_id = ?", (ids["case"],)).fetchall()
    assert [tuple(r) for r in rows] == [(ids["jjh"], "2026-08-18", "RATEREQUESTTPEIAH", "CONV-DEMO-OFFICE-ASK")]
    assert conn.execute("SELECT requester_party_id FROM enquiry WHERE id = ?",
                        (ids["case"],)).fetchone()[0] == ids["challenge"]        # untouched
    conn.close()
    body = client.post(url, follow_redirects=True).data.decode("utf-8")
    assert "already on the case: JoJo JJH" in body
    conn = db.open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM vendor_request WHERE enquiry_id = ?", (ids["case"],)).fetchone()[0] == 1
    conn.close()


def test_the_vendors_route_creates_a_missing_office_as_a_own_office(world, monkeypatch):
    conn, path, ids = world
    conn.close()
    mail = jjh_mail()
    mail.to_names = ["JQB-ANAN P"]
    client = _client(path, monkeypatch, mail)
    body = client.post("/enquiries/{}/vendors/from-outlook".format(ids["case"]),
                       follow_redirects=True).data.decode("utf-8")
    assert "Logged 1 vendor request: JoJo JQB" in body
    conn = db.open_db(path)
    party = conn.execute("SELECT id, type, country FROM party WHERE key = 'JoJo JQB'").fetchone()
    assert party["type"] == "Office"
    assert conn.execute("SELECT COUNT(*) FROM party_domain WHERE party_id = ?", (party["id"],)).fetchone()[0] == 0
    assert conn.execute("SELECT vendor_party_id FROM vendor_request WHERE enquiry_id = ?",
                        (ids["case"],)).fetchone()[0] == party["id"]
    conn.close()


def test_the_archive_backed_mail_offers_the_same_office(world, monkeypatch, tmp_path):
    conn, path, ids = world
    conn.close()
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(jjh_mail())])
    aconn.commit()
    aconn.close()
    with mail_archive.ArchiveSearch(archive) as search:
        stored = search.by_message_id("<office-ask-0818@jojofreight.example>")
    assert stored.to_names == ["JJH-DANIEL YIP", "JJH-GLORIA TAM", "JJH-KELVIN HO"] and stored.cc_names == []
    conn = db.open_db(path)
    assert [v["key"] for v in intake.build(conn, stored).vendors] == ["JoJo JJH"]
    conn.close()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    body = client.post("/enquiries/{}/vendors/from-outlook".format(ids["case"]),
                       data={"message_id": "<office-ask-0818@jojofreight.example>"},
                       follow_redirects=True).data.decode("utf-8")
    assert "Logged 1 vendor request: JoJo JJH" in body


def test_register_from_outlook_shares_the_offer(world, monkeypatch):
    conn, path, ids = world
    conn.close()
    client = _client(path, monkeypatch, jjh_mail())
    body = client.get("/enquiries/from-outlook").data.decode("utf-8")
    assert 'name="vendor" value="office:JJH" checked' in body
    assert 'name="vendor_key_office:JJH" value="JoJo JJH"' in body
    assert "own office JJH" in body and "JQM copied, not offered" in body
