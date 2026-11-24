"""Batch T5b: replies that ask us to act count as asking; a recall notice and
a 'proceed with' that is their own next step are not closings; every link
in angle brackets (any scheme) and [cid:...] is out of the words.

Every company, person, mail and link here is invented; cases from
OPP-7801.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, mail_archive, parsing, propose
from opplist.outlook_bridge import MailInfo

AGENT = ("ilse@quarrybend.test", "Ilse Varga")
US = ("desk@jojofreight.example", "JJC-DESK")
SUBJECT = "RATE REQUEST // MIA - TALCA // AIR"


# ------------------------------------------------------------ reply_kind --
@pytest.mark.parametrize("text", [
    "Please also check that the importer's licence still covers this kind of cargo",
    "Kindly also confirm the cut-off.",
    "Yes, please proceed with the Friday uplift and send us the flight number today",
    "Please book it on the Thursday flight.",
    "Kindly arrange the pickup for Monday.",
    "Please go ahead.",
])
def test_a_request_for_us_to_act_asks(text):
    assert detect_followups.reply_kind(text) == "asks"


@pytest.mark.parametrize("text", [
    "We will put a dash in the empty boxes to proceed with customer submission",
    "Thanks, we will proceed with the payment tomorrow.",
    "Well received, thank you.",
])
def test_their_own_next_step_does_not_ask(text):
    assert detect_followups.reply_kind(text) == "noted"


def test_a_question_mark_inside_a_link_is_no_question_and_no_link_shows():
    text = "Thanks Rafe <blob:outlook-mobile-compose:/a1b2?x=1> [cid:image001.png@01DA?] noted"
    assert detect_followups.reply_kind(text) == "noted"
    assert detect_followups.without_links(text) == "Thanks Rafe noted"
    for other in ("<cid:logo?1>", "<tel:+1-555-0100?ext=2>", "<file://srv/share/a?.pdf>"):
        assert detect_followups.reply_kind("Received " + other) == "noted", other
        assert detect_followups.without_links("Received " + other) == "Received"
    # a remark in angle brackets with spaces is words, not a link
    assert detect_followups.reply_kind("<Note: could you check the date?>") == "asks"


# ------------------------------------------------- the draft is held --
def _mail(at, sender, body, mid, internal=False, to=(US[0],)):
    subject = ("RE: [OPP-7801] " if internal else "") + SUBJECT
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, sent_at=at, entry_id="EID-" + mid,
                    conversation_id="CONV-7801", conversation_topic=parsing.clean_subject(SUBJECT),
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=list(to), body_excerpt=body, folder_path="\\Mailbox\\Inbox",
                    display_to="; ".join(to), message_id="<{}@x>".format(mid))


def test_please_proceed_asks_and_the_scan_writes_it_as_noted(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    conn = db.open_db(tmp_path / "t5b.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Quarrybend Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "quarrybend.test", "party_id": agent})
    case = add_enquiry(repo, "OPP-7801", agent, received="2026-11-02", source_subject=SUBJECT,
                       thread_key=parsing.subject_thread_key(SUBJECT), entry_id="EID-i1",
                       conversation_id="CONV-7801", service_mode="Air", origin_country="USA", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-11-03")
    repo.commit()
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in (
        _mail("2026-11-02 09:00", AGENT, "Hi,\nPlease quote air 3 crates.", "i1"),
        _mail("2026-11-03 10:00", US, "Hi Ilse,\nOur rate is USD 980 all-in.\nRafe", "q1", internal=True,
              to=(AGENT[0],)),
        _mail("2026-11-05 09:00", AGENT, "Hi Rafe,\nYes, please proceed with the Friday uplift and send us the "
                                         "flight number today.\nIlse", "r1"))])
    aconn.commit()
    aconn.close()
    with mail_archive.ArchiveSearch(archive) as search:
        detect_followups.run(conn, search, opp_nos=["OPP-7801"])
    note = conn.execute("SELECT note FROM status_event WHERE enquiry_id = ? AND note LIKE 'Reply from%'",
                        (case,)).fetchone()["note"]
    # T5b: a request for us to act asks; FX4: the scan writes it as noted all the same
    assert detect_followups.reply_kind("Yes, please proceed with the Friday uplift and send us the flight number today.") == "asks"
    assert note.startswith("Reply from Ilse Varga (noted):")
    assert not hasattr(detect_followups, "latest_reply") and not hasattr(detect_followups, "holds_draft")
    conn.close()


# ------------------------------------------------------------ propose --
def _thread(body, subject="RE: " + SUBJECT):
    return [propose.ThreadMail(at="2026-11-06", sender_name="Ilse Varga", address=AGENT[0], internal=False,
                               subject=subject, body=body, from_requester=True)]


@pytest.mark.parametrize("subject, body", [
    ("Recall: " + SUBJECT, "Ilse Varga would like to recall the message, \"RATE REQUEST\"."),
    ("Cancelar: " + SUBJECT, "Ilse Varga deseja cancelar a mensagem \"RATE REQUEST\"."),
    ("RE: " + SUBJECT, "Ilse Varga gostaria de cancelar a mensagem \"RATE REQUEST\"."),
    ("RE: " + SUBJECT, "Ilse Varga desea recuperar el mensaje \"RATE REQUEST\"."),
])
def test_a_recall_notice_gives_no_cancelled(subject, body):
    assert propose.classify(_thread(body, subject), "Quarrybend Cargo", requester_known=True).proposed_status \
        != "Cancelled"


def test_a_cancel_that_is_not_a_recall_still_gives_cancelled():
    got = propose.classify(_thread("The consignee cancelled the order, sorry."), "Quarrybend Cargo",
                           requester_known=True)
    assert got.proposed_status == "Cancelled"


@pytest.mark.parametrize("body, won", [
    ("We will put a dash in the remaining customs boxes to proceed with customer submission.",
     False),
    ("We will proceed with client approval next week.", False),
    ("Yes, please proceed with the Friday uplift and send us the flight number.", True),
    ("Our client is happy, so we proceed with the booking.", True),
])
def test_proceed_with_their_own_step_is_not_won(body, won):
    got = propose.classify(_thread(body), "Quarrybend Cargo", requester_known=True)
    assert (got.proposed_status == "Won") is won
