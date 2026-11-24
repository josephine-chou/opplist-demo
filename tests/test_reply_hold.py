"""Batch T5: a reply already on the timeline that asks nothing does not hold
the follow-up draft; links are taken out of a reply's first words. FX4
(2026-11-12): the draft and its hold are gone; the links test stays.

detect_followups.holds_draft is the one rule for the case page's card,
Tracking's rows and Open draft in Outlook. The dates are counted back
from today (Tracking reads the real date). Every company, person, mail and link here
is invented; cases from OPP-7701.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import dates, db, detect_followups, mail_archive, outlook_bridge, parsing
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app
from test_acknowledge import FakeApplication

TODAY = dt.date.today()
AGENT = ("wren@halyardcargo.test", "Wren Okafor")
US = ("desk@jojofreight.example", "JJC-DESK")
#: a link the way a mail security scanner rewrites it
SCANNED = "<https://secure-web.linkguard.test/v1/Zq9-xT_4/https%3A%2F%2Fhalyardcargo.test%2Ftrack>"
NOTED = "Thank you Tomas!! {}\nWren".format(SCANNED)
ASKS = "Hi Tomas,\nCould you hold the space until Friday?\nWren"
OPPS = ("OPP-7701", "OPP-7702", "OPP-7703", "OPP-7704")


def ago(days, hour=9):
    return "{} {:02d}:00".format((TODAY - dt.timedelta(days=days)).isoformat(), hour)


def mail(opp, at, sender, body, mid, internal=False, to=(US[0],)):
    subject = "RATE REQUEST // {} // AIR".format(opp)
    return MailInfo(subject=("RE: [{}] ".format(opp) if internal else "") + subject, received_on=at[:10],
                    received_at=at, entry_id="EID-" + mid, conversation_id="CONV-" + opp,
                    conversation_topic=parsing.clean_subject(subject), sender_name=sender[1],
                    sender_address=sender[0], sender_internal=internal, to_addresses=list(to), body_excerpt=body,
                    folder_path="\\Mailbox\\Inbox", display_to="; ".join(to), message_id="<{}@x>".format(mid))


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "t5.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    halyard = add_party(repo, "Halyard Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "halyardcargo.test", "party_id": halyard})
    ids, mails = {}, []
    # 7701: a thank-you, on the timeline; 7702: a question, on the
    # timeline; 7703: a thank-you the scan has not read yet; 7704: a
    # thank-you whose note was struck out
    for opp, body in zip(OPPS, (NOTED, ASKS, NOTED, NOTED)):
        subject = "RATE REQUEST // {} // AIR".format(opp)
        ids[opp] = add_enquiry(repo, opp, halyard, received=ago(15)[:10], source_subject=subject,
                               thread_key=parsing.subject_thread_key(subject), entry_id="EID-i" + opp,
                               conversation_id="CONV-" + opp, service_mode="Air", origin_country="USA",
                               dest_country="Chile", created_by="outlook")
        repo.set_status(ids[opp], "Quoted", ago(14)[:10])
        mails += [mail(opp, ago(15), AGENT, "Hi,\nPlease quote air MIA - SCL, 4 crates, 260 kg.", "i" + opp),
                  mail(opp, ago(14, 10), US, "Hi Wren,\nOur rate is USD 1,180 all-in.\nTomas", "q" + opp,
                       internal=True, to=(AGENT[0],)),
                  mail(opp, ago(8, 15), AGENT, body, "r" + opp)]
    repo.commit()
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(aconn, "last_sync_at", TODAY.isoformat() + " 07:00:00")
    aconn.commit()
    aconn.close()
    with mail_archive.ArchiveSearch(archive) as search:
        detect_followups.run(conn, search, opp_nos=["OPP-7701", "OPP-7702", "OPP-7704"])
    struck = conn.execute("SELECT id FROM status_event WHERE enquiry_id = ? AND note LIKE 'Reply from%'",
                          (ids["OPP-7704"],)).fetchone()["id"]
    repo.update("status_event", struck, {"voided": 1})
    repo.commit()
    monkeypatch.setattr(outlook_bridge, "_com", contextlib.nullcontext)
    monkeypatch.setattr(outlook_bridge, "_application", lambda: FakeApplication({}))
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "ids": ids, "archive": archive, "web": app.test_client()}
    conn.close()


def latest(w, opp):
    row = w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (w["ids"][opp],)).fetchone()
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        return detect_followups.latest_reply(w["conn"], search, row)


def case_page(w, opp):
    return w["web"].get("/enquiries/{}".format(w["ids"][opp])).data.decode("utf-8")


# ------------------------------------------------------------- the rule --
# ------------------------------------------------------- the case page --
# ------------------------------------------------------------- Tracking --
# ---------------------------------------------------------------- links --
def test_links_are_taken_out_of_a_new_reply_note_and_an_answer_note(world):
    w = world
    note = w["conn"].execute("SELECT note FROM status_event WHERE enquiry_id = ? AND note LIKE 'Reply from%'",
                             (w["ids"]["OPP-7701"],)).fetchone()["note"]
    assert note == "Reply from Wren Okafor (noted): Thank you Tomas!! Wren [mid:<rOPP-7701@x>]"
    words = "See https://halyardcargo.test/a?b=1 and <mailto:wren@halyardcargo.test> , thanks"
    assert detect_followups.reply_note("Wren", words) == "Reply from Wren (noted): See and , thanks"
    assert detect_followups.answer_note("Tomas", "Done <http://x.test/y> today") == "Answered by Tomas: Done today"
    # cut to length after the links are out
    long = SCANNED + " " + "word " * 40
    assert detect_followups.reply_note("Wren", long).split(": ", 1)[1].startswith("word word")
