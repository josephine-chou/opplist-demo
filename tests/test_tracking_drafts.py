"""Batch F2: Open draft in Outlook from the Tracking rows (the morning drafts
by Claude went in CX1, 2026-11-04: a drafts file left on disk is not read).
FX4 (2026-11-12): Open draft in Outlook and its route are gone; the rows
keep Log as sent, Outlook is never touched.

The fake Outlook of the Acknowledge tests (Send fails the test) and a fake
archive. The dates are counted back from today: the Tracking page reads
the real date. Every company,
person, mail and number here is invented; cases from OPP-4901.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, followup, mail_archive, outlook_bridge, parsing, paths
from opplist.outlook_bridge import MailInfo
from opplist.web import create_app
from test_acknowledge import FakeApplication, FakeMail

TODAY = dt.date.today()
AGENT = ("rosalind@kittiwakecargo.example", "Rosalind Fenn")
US = ("desk@jojofreight.example", "JJC-DESK")


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
    path = tmp_path / "f2.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    kitti = add_party(repo, "Kittiwake Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "kittiwakecargo.example", "party_id": kitti})
    plover = add_party(repo, "Plover Plastics", ptype="Direct Customer", country="USA")
    repo.insert("party_domain", {"domain": "ploverplastics.example", "party_id": plover})
    mails, ids = [], {}

    def case(opp, quoted_days, fu_days=None, party=kitti, reply_days=None, sender=AGENT):
        subject = "RATE REQUEST // {} // AIR".format(opp)
        ids[opp] = add_enquiry(repo, opp, party, received=ago(quoted_days + 1)[:10], source_subject=subject,
                               thread_key=parsing.subject_thread_key(subject), entry_id="EID-i" + opp,
                               conversation_id="CONV-" + opp, service_mode="Air", origin_country="USA",
                               dest_country="Chile", created_by="outlook")
        repo.set_status(ids[opp], "Quoted", ago(quoted_days)[:10])
        mails.append(mail(opp, ago(quoted_days + 1), sender, "Hi,\nPlease quote air MIA - SCL, 5 crates, 300 kg.",
                          "i" + opp))
        mails.append(mail(opp, ago(quoted_days, 10), US, "Hi,\nOur rate is USD 1,200 all-in.\nRegards", "q" + opp,
                          internal=True, to=(sender[0],)))
        if fu_days is not None:
            repo.add_followup(ids[opp], ago(fu_days)[:10])
            mails.append(mail(opp, ago(fu_days, 11), US, "Hi,\nJust checking back on our quote.\nThank you.",
                              "f" + opp, internal=True, to=(sender[0],)))
        if reply_days is not None:
            mails.append(mail(opp, ago(reply_days, 15), sender, "Hi,\nStill with the client.\nRosalind", "r" + opp))

    case("OPP-4901", 6)                                  # FU1 due
    case("OPP-4902", 30, fu_days=15)                     # FU2 due
    # L2: FU2 only after an answer to FU1 - her note of a call
    repo.add_note(ids["OPP-4902"], ago(14)[:10], "Phoned Rosalind: the client is still comparing")
    case("OPP-4903", 6, reply_days=2)                    # FU1 due, but they wrote back
    case("OPP-4904", 6, party=plover, sender=("ines@ploverplastics.example", "Ines Plover"))     # direct customer
    repo.commit()
    aconn = mail_archive.open_archive(archive)
    mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in mails])
    mail_archive.set_meta(aconn, "last_sync_at", TODAY.isoformat() + " 07:00:00")
    aconn.commit()
    aconn.close()
    outlook = {}
    monkeypatch.setattr(outlook_bridge, "_com", contextlib.nullcontext)
    monkeypatch.setattr(outlook_bridge, "_application", lambda: FakeApplication(outlook))
    app = create_app(path)
    app.config["TESTING"] = True
    yield {"conn": conn, "repo": repo, "ids": ids, "outlook": outlook, "web": app.test_client()}
    conn.close()


def tracking(w):
    return w["web"].get("/tracking").data.decode("utf-8")


# ------------------------------------------------------- 1. the Tracking rows --
def test_the_row_has_no_draft_and_its_route_is_gone(world):
    w = world
    page = tracking(w)
    assert 'form="fu-outlook-{}"'.format(w["ids"]["OPP-4901"]) not in page and "Open draft in Outlook" not in page
    fu1 = page[page.index('id="fu1"'):]
    assert 'value="followup"' in fu1[:fu1.index("</section>")]                  # Log as sent stays
    w["outlook"]["EID-qOPP-4901"] = FakeMail("RE: [OPP-4901] RATE REQUEST // OPP-4901 // AIR", sender="JJC-DESK")
    reply = w["web"].post("/tracking/fu-outlook/{}".format(w["ids"]["OPP-4901"]), data={"step": "1", "card": "fu1"})
    assert reply.status_code == 404
    assert w["outlook"]["EID-qOPP-4901"].replies == []
    # nothing was written to the case
    assert w["conn"].execute("SELECT COUNT(*) FROM status_event WHERE enquiry_id = ? AND kind = 'followup'",
                             (w["ids"]["OPP-4901"],)).fetchone()[0] == 0


def test_a_drafts_file_left_on_disk_is_not_read(world):
    r"""CX1: data\claude-work\fu-drafts.json stays on disk, unread: Tracking
    has no drafts line and the case page shows none of it."""
    w = world
    folder = paths.DATA_DIR / "claude-work"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fu-drafts.json").write_text(json.dumps({"date": TODAY.isoformat(), "drafts": [
        {"case": "OPP-4901", "step": 1, "text": "Hi Rosalind,\n\nAn old draft from the file.", "line": "Draft by Claude"}]}),
        encoding="utf-8")
    page = tracking(w)
    assert "Drafts by Claude" not in page and "An old draft from the file." not in page and "morning draft" not in page
    body = w["web"].get("/enquiries/{}".format(w["ids"]["OPP-4901"])).data.decode("utf-8")
    assert "An old draft from the file." not in body and "Draft by Claude" not in body


def test_a_row_whose_requester_wrote_back_has_no_button_and_no_hold_line(world):
    w = world
    page = tracking(w)
    case = w["ids"]["OPP-4903"]
    assert 'form="fu-outlook-{}"'.format(case) not in page
    assert "they wrote back {} - read it first".format(ago(2)[:10]) not in page
    assert w["web"].post("/tracking/fu-outlook/{}".format(case), data={"step": "1", "card": "fu1"}).status_code == 404
