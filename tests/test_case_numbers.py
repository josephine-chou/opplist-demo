"""AL1 (2026-10-01): case numbers in mail subjects. The parser, which case a
mail belongs to (the registered mail, one number, two numbers, a number
that does not fit), the acknowledgement event, the register page and the
case number the case header copies.

Every company, person, mail, subject, lane and date here is invented; cases
from OPP-2911."""

from __future__ import annotations

import re

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import case_mail, case_numbers, db, detect_followups, mail_archive, parsing, propose, suggestions
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app

SUBJECT = "RATE REQUEST // TALCA - HOUSTON // FCL"


# ------------------------------------------------------------- 1. parser --
@pytest.mark.parametrize("subject,numbers", [
    ("[OPP-2911] RE: " + SUBJECT, ["OPP-2911"]),
    ("RE: [OPP-2911] " + SUBJECT, ["OPP-2911"]),
    ("RE: [ opp 2911 ] " + SUBJECT, ["OPP-2911"]),
    ("RE: OPP-2911 " + SUBJECT, ["OPP-2911"]),
    ("RE: OPP 2911 " + SUBJECT, ["OPP-2911"]),
    ("re: [opp-1504] cotizacion // curico - akron", ["OPP-1504"]),
    ("RE: [OPP-2911][OPP-2912] " + SUBJECT, ["OPP-2911", "OPP-2912"]),
    ("RE: [OPP-2911] FW: [OPP-2911] " + SUBJECT, ["OPP-2911"]),
    ("OPPORTUNITY TO QUOTE // TALCA", []),
    ("XOPP-2911 " + SUBJECT, []),
    ("OPP-29110 " + SUBJECT, []),
    (SUBJECT, []),
])
def test_the_case_numbers_a_subject_carries(subject, numbers):
    assert parsing.case_numbers(subject) == numbers


def test_a_numbered_subject_cleans_to_the_same_subject_and_thread_key():
    keys = {parsing.subject_thread_key(s) for s in (
        SUBJECT, "RE: " + SUBJECT, "RE: [OPP-2911] " + SUBJECT, "[OPP-2911] RE: " + SUBJECT,
        "RE: [OPP-2911] [OPP-2912] " + SUBJECT, "RE: opp 2911 " + SUBJECT)}
    assert keys == {"RATEREQUESTTALCAHOUSTONFCL"}
    assert parsing.clean_subject("RE: [OPP-2911] " + SUBJECT) == SUBJECT
    assert parsing.clean_subject("[OPP-2911] RE: " + SUBJECT) == SUBJECT


# --------------------------------------------------------- the archive --
def mail(subject, at, sender, name, body, conversation, to, mid, internal=False, entry=None, to_names=()):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id=entry or "EID-" + mid,
                    conversation_id=conversation, conversation_topic=parsing.clean_subject(subject),
                    sender_name=name, sender_address=sender, sender_internal=internal,
                    to_addresses=list(to), to_names=list(to_names), body_excerpt=body,
                    folder_path="\\\\Personal Folder\\\\AGENTS", display_to="; ".join(to), message_id="<{}@x>".format(mid))


ANA = ("ana@marlowfreight.example", "Ana Ruiz")
US = ("desk@jojofreight.example", "JJC-DESK")
MAILS = [
    # the inquiry, registered as OPP-2911
    mail(SUBJECT, "2026-11-02 09:00", ANA[0], ANA[1], "Hi,\n\nPlease quote 1 x 40HQ Talca - Houston.\n\nAna",
         "CONV-A", [US[0]], "m1", entry="EID-REG-2911"),
    # our acknowledgement: the lane and '1 x 40HQ', no price
    mail("RE: [OPP-2911] " + SUBJECT, "2026-11-02 10:15", US[0], US[1],
         "Hi Ana,\n\nWell received, we are checking 1 x 40HQ Talca - Houston and revert shortly.\n\nRegards,\nDesk",
         "CONV-A", [ANA[0]], "m2", internal=True),
    # their reply with the number, in a new conversation
    mail("RE: [OPP-2911] " + SUBJECT, "2026-11-03 08:40", ANA[0], ANA[1],
         "Hi,\n\nThe cargo is ready on the 10th.\n\nAna", "CONV-NEW", [US[0]], "m3"),
    # two numbers
    mail("RE: [OPP-2911] [OPP-2912] " + SUBJECT, "2026-11-03 11:00", ANA[0], ANA[1],
         "Hi,\n\nOne more question.\n\nAna", "CONV-A", [US[0]], "m4"),
    # our mail to another agent with OPP-2911's number pasted by mistake
    mail("[OPP-2911] AIR QUOTE // MOLINA - DAYTON", "2026-11-03 15:00", US[0], US[1],
         "Hi Tomas,\n\nPlease find our rates.\n\nRegards", "CONV-B", ["tomas@pelicanlines.example"], "m5", internal=True),
    # an agent asking for something new inside OPP-2911's thread: registered as a new case
    mail("RE: [OPP-2911] " + SUBJECT, "2026-11-04 09:30", ANA[0], ANA[1],
         "Hi,\n\nCan you also quote 2 x 20GP Talca - Savannah?\n\nAna", "CONV-A", [US[0]], "m7", entry="EID-NEW"),
]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    path = tmp_path / "archive" / "mail.db"
    path.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", path)
    conn = mail_archive.open_archive(path)
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in MAILS])
    mail_archive.set_meta(conn, "last_sync_at", "2026-11-04 12:00:00")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def world(tmp_path, archive):
    path = tmp_path / "numbers.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    marlow = add_party(repo, "Marlow Freight", country="Chile")
    repo.insert("party_domain", {"domain": "marlowfreight.example", "party_id": marlow})
    pelican = add_party(repo, "Pelican Lines", country="USA")
    repo.insert("party_domain", {"domain": "pelicanlines.example", "party_id": pelican})
    ids = {"marlow": marlow, "pelican": pelican}
    ids["a"] = add_enquiry(repo, "OPP-2911", marlow, received="2026-11-02", source_subject=SUBJECT,
                           thread_key=parsing.subject_thread_key(SUBJECT),
                           entry_id="EID-REG-2911", conversation_id="CONV-A", service_mode="FCL")
    ids["b"] = add_enquiry(repo, "OPP-2912", pelican, received="2026-11-02",
                           source_subject="AIR QUOTE // MOLINA - DAYTON", conversation_id="CONV-B", service_mode="Air")
    db.set_setting(conn, "next_opp_number", 2913)
    repo.commit()
    yield conn, path, ids
    conn.close()


def keys(mails):
    return sorted(m.message_id for m in mails)


def card(conn, archive, enquiry_id):
    row = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (enquiry_id,)).fetchone()
    with mail_archive.ArchiveSearch(archive) as search:
        return keys(case_mail.case_mails(conn, search, row))


# ------------------------------------------------- 4. which case a mail is --
def test_one_number_two_numbers_and_a_number_that_does_not_fit(world, archive):
    conn, path, ids = world
    # m3 is in a new conversation: its number brings it in; m4 (two numbers) and
    # m5 (our mail to another agent) are no case's; m7 is still OPP-2911's for now
    assert card(conn, archive, ids["a"]) == ["<m1@x>", "<m2@x>", "<m3@x>", "<m7@x>"]
    assert card(conn, archive, ids["b"]) == []
    with mail_archive.ArchiveSearch(archive) as search:
        by_mid = {m.message_id: m for m in search.by_subject("TALCA", limit=50) + search.by_subject("MOLINA", limit=50)}
        assert case_numbers.link_of(conn, by_mid["<m3@x>"], search).kind == "number"
        assert case_numbers.link_of(conn, by_mid["<m4@x>"], search).kind == "numbers"
        assert case_numbers.link_of(conn, by_mid["<m5@x>"], search).kind == "mismatch"
        assert case_numbers.link_of(conn, by_mid["<m1@x>"], search).kind == "registered"
        member = suggestions.membership(conn, {"id": ids["b"]}, by_mid["<m4@x>"])
        assert not member.belongs and "two case numbers" in member.how


def test_a_new_case_registered_from_an_old_number_never_shares_the_thread(world, archive):
    conn, path, ids = world
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    page = client.post("/enquiries/new", data={
        "received_on": "2026-11-04", "requester_party_id": str(ids["marlow"]), "service_mode": "FCL",
        "origin_country": "Chile", "dest_country": "USA", "created_by": "outlook", "entry_id": "EID-NEW",
        "conversation_id": "CONV-A", "thread_key": "RATEREQUESTTALCAHOUSTONFCL",
        "source_subject": "RE: [OPP-2911] " + SUBJECT, "decision": "new", "form_token": "tok-new",
        "cargo_0_container_qty": "2", "cargo_0_container_type": "20GP", "cargo_0_counted_present": "1",
        "cargo_0_counted": "1"})
    assert page.status_code == 302, page.data.decode("utf-8")[:2000]
    new = conn.execute("SELECT * FROM enquiry WHERE opp_no = 'OPP-2913'").fetchone()
    assert (new["entry_id"], new["thread_key"], new["conversation_id"]) == ("EID-NEW", None, None)
    # the new case has its registered mail and nothing of the old thread;
    # the old case loses that one mail and keeps the rest
    assert card(conn, archive, new["id"]) == ["<m7@x>"]
    assert card(conn, archive, ids["a"]) == ["<m1@x>", "<m2@x>", "<m3@x>"]
    old = conn.execute("SELECT thread_key, conversation_id FROM enquiry WHERE id = ?", (ids["a"],)).fetchone()
    assert tuple(old) == ("RATEREQUESTTALCAHOUSTONFCL", "CONV-A")


def test_a_numbered_mail_never_lands_on_another_case_sharing_its_thread_key(world, archive, tmp_path):
    conn, path, ids = world
    repo = db.Repository(conn, source="test")
    # another case of the same agent with the same subject (an old one)
    other = add_enquiry(repo, "OPP-2920", ids["marlow"], received="2026-11-01", source_subject=SUBJECT,
                        thread_key=parsing.subject_thread_key(SUBJECT))
    repo.commit()
    assert conn.execute("SELECT thread_key FROM enquiry WHERE id = ?", (other,)).fetchone()[0] == \
        "RATEREQUESTTALCAHOUSTONFCL"
    mails = card(conn, archive, other)
    assert "<m3@x>" not in mails and "<m2@x>" not in mails and "<m4@x>" not in mails
    assert mails == ["<m1@x>"] or mails == []        # m1 carries no number: today's rules


# ---------------------------------------- 4c, 5, 6: the scan and its findings --
def test_the_scan_suggests_and_acknowledges_once(world, archive):
    conn, path, ids = world
    with mail_archive.ArchiveSearch(archive) as search:
        report = detect_followups.run(conn, search)
    found = report.case_numbers
    assert found.acknowledged and found.acknowledged[0].startswith("OPP-2911 2026-11-02")
    rows = {r["proposed_status"]: r for r in conn.execute(
        "SELECT * FROM suggestion WHERE kind = 'case_number' ORDER BY id")}
    assert set(rows) == {case_numbers.TWO_NUMBERS, case_numbers.CHECK_NUMBER}
    two = rows[case_numbers.TWO_NUMBERS]
    assert (two["named_cases"], two["opp_no"], two["message_id"]) == ("OPP-2911,OPP-2912", "OPP-2911", "<m4@x>")
    check = rows[case_numbers.CHECK_NUMBER]
    assert (check["named_cases"], check["evidence_sender"]) == ("OPP-2911", "to tomas@pelicanlines.example")
    assert check["evidence_quote"] == "[OPP-2911] AIR QUOTE // MOLINA - DAYTON"
    ack = conn.execute("SELECT * FROM status_event WHERE kind = 'ack'").fetchall()
    assert [(a["enquiry_id"], a["at"], a["status"]) for a in ack] == [(ids["a"], "2026-11-02", "New")]
    case = conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids["a"],)).fetchone()
    assert (case["status"], case["quoted_on"], case["fu_count"], case["quote_rounds"]) == ("New", None, 0, 0)
    # a second run writes nothing new
    with mail_archive.ArchiveSearch(archive) as search:
        again = detect_followups.run(conn, search).case_numbers
    assert again.acknowledged == [] and again.suggested == []
    assert conn.execute("SELECT COUNT(*) FROM status_event WHERE kind = 'ack'").fetchone()[0] == 1
    # the acknowledgement is neither a quote nor a follow-up for propose
    with mail_archive.ArchiveSearch(archive) as search:
        [proposal] = propose.propose(conn, search, opp_nos=["OPP-2911"], scope="all")
    assert proposal.proposed_status != "Quoted"
    assert "<m2@x>" not in [getattr(m, "message_id", "") for m in proposal.mails]


def test_attaching_the_two_number_mail_to_one_case(world, archive):
    conn, path, ids = world
    with mail_archive.ArchiveSearch(archive) as search:
        detect_followups.run(conn, search)
    two = conn.execute("SELECT id FROM suggestion WHERE proposed_status = ?", (case_numbers.TWO_NUMBERS,)).fetchone()[0]
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    # T1: worked on the case page of the first number, not on Tracking
    first = conn.execute("SELECT enquiry_id FROM suggestion WHERE id = ?", (two,)).fetchone()[0]
    body = client.get("/enquiries/{}".format(first)).data.decode("utf-8")
    assert "Attach to OPP-2911" in body and "Attach to OPP-2912" in body and "Ignore" in body
    assert "Attach to OPP-2911" not in client.get("/tracking").data.decode("utf-8")
    client.post("/suggestions/{}/accept".format(two), data={"case": "OPP-2912", "next": "/enquiries/{}".format(first)})
    assert card(conn, archive, ids["b"]) == ["<m4@x>"]
    assert "<m4@x>" not in card(conn, archive, ids["a"])
    assert conn.execute("SELECT state FROM suggestion WHERE id = ?", (two,)).fetchone()[0] == "accepted"


# ------------------------------------------------------- C. the register page --
def test_the_register_check_names_the_case_from_the_number(world, archive, monkeypatch):
    conn, path, ids = world
    from opplist import outlook_bridge
    monkeypatch.setattr(outlook_bridge, "read_selection", lambda: MAILS[5])
    app = create_app(path)
    app.config["TESTING"] = True
    body = app.test_client().post("/enquiries/from-outlook").data.decode("utf-8")
    # DD1: the thread's case is a row of "Already in OppList?"
    banner = body[body.index('id="already"'):body.index("</section>", body.index('id="already"'))]
    assert ">OPP-2911</a>" in banner and "Marlow Freight" in banner and "received" in banner
    # DD1: an open case's row offers Update; its number opens it in a new tab
    assert "case number in the subject" in banner and "Update OPP-2911 from this mail" in banner
    assert re.search(r'<a href="/enquiries/\d+" target="_blank">OPP-2911</a>', banner)
    form = body[body.index('id="enquiry-form"'):]
    assert 'name="thread_key" value=""' in form and 'name="conversation_id" value=""' in form


def test_a_new_case_starts_with_owner_empty(world):
    conn, path, ids = world
    db.set_setting(conn, "last_owner", "Victor")
    conn.commit()
    app = create_app(path)
    app.config["TESTING"] = True
    client = app.test_client()
    page = client.post("/enquiries/new", data={
        "received_on": "2026-11-05", "requester_party_id": str(ids["pelican"]), "service_mode": "Air",
        "origin_country": "USA", "origin_loc": "MIA", "dest_country": "Chile", "dest_loc": "SAI",
        "owner": "Ana", "decision": "new", "form_token": "tok-owner",
        "cargo_0_weight_value": "120", "cargo_0_weight_unit": "kg", "cargo_0_counted_present": "1", "cargo_0_counted": "1"})
    assert page.status_code == 302, page.data.decode("utf-8")[:2000]
    assert conn.execute("SELECT owner FROM enquiry WHERE opp_no = 'OPP-2913'").fetchone()[0] is None
    assert db.get_setting(conn, "last_owner") == "Victor"          # nothing remembered from this one


# ----------------------------------------------------------- D. the copy --
def test_the_case_number_in_the_header_copies_the_bracketed_number(world):
    conn, path, ids = world
    app = create_app(path)
    app.config["TESTING"] = True
    body = app.test_client().get("/enquiries/{}".format(ids["a"])).data.decode("utf-8")
    # WA1: the number leads the h1, the sub line starts with the requester
    h1 = body[body.index("<h1>"):body.index("</h1>")]
    assert 'data-copy-opp="[OPP-2911] "' in h1 and 'title="Click to copy [OPP-2911]"' in h1
    assert 'class="opp-copied" aria-live="polite" hidden>Copied<' in h1
    # numbers in lists still open the case
    listing = app.test_client().get("/enquiries?view=all").data.decode("utf-8")
    assert "data-copy-opp" not in listing
