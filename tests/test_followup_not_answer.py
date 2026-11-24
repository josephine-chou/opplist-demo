"""Batch T4b: a follow-up already logged (by hand that day, or on the team
site within a day) is still a follow-up when its mail comes in, so AL6a
never reads it as the answer to the requester's question; and a struck-out
follow-up no longer counts in the ladder. FX4 (2026-11-12): the AL6a step
is gone - no mail of ours is read as the answer, only Answered clears a
question - and the scan's replies never ask.

Every company, person, mail, place and date here is invented; cases from
OPP-7501.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import assign_list, db, detect_followups, mail_archive, parsing, tracking
from opplist.outlook_bridge import MailInfo

SUBJECT = "RATE REQUEST // CORVEN - DALBY // 2 PLT"
AGENT = ("noor@fenwickcargo.test", "Noor Allard")
US = ("tam@jojofreight.example", "JJC-TAM RIGG")
QUESTION = "Can you confirm the rate covers the liftgate at Dalby?"


def mail(at, sender, body, mid, internal=False, to=(AGENT,)):
    return MailInfo(subject="RE: " + SUBJECT, received_on=at[:10], received_at=at, sent_at=at, entry_id="EID-" + mid,
                    conversation_id="CONV-7501", conversation_topic=SUBJECT, sender_name=sender[1],
                    sender_address=sender[0], sender_internal=internal, to_addresses=[a for a, _ in to],
                    body_excerpt=body, folder_path="\\Mailbox\\Inbox", display_to="; ".join(n for _, n in to),
                    message_id="<{}@x>".format(mid))


QUOTE = mail("2026-11-03 10:00", US, "Hi Noor,\nOur rate is USD 410 all-in.\nTam", "q1", internal=True)
CHASE = "Hi Noor,\nChecking back about our offer, any update?\nTam"


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    conn = db.open_db(tmp_path / "t4b.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Fenwick Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "fenwickcargo.test", "party_id": agent})
    case = add_enquiry(repo, "OPP-7501", agent, received="2026-11-02", source_subject=SUBJECT,
                       thread_key=parsing.subject_thread_key(SUBJECT), conversation_id="CONV-7501",
                       service_mode="LCL", origin_country="USA", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-11-03")
    repo.commit()
    yield {"conn": conn, "repo": repo, "case": case, "archive": archive}
    conn.close()


def store(w, *mails):
    conn = mail_archive.open_archive(w["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def scan(w):
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        return detect_followups.run(w["conn"], search)


def notes(w):
    return [r[0] for r in w["conn"].execute(
        "SELECT note FROM status_event WHERE enquiry_id = ? AND kind = 'note' AND voided = 0 ORDER BY at, id",
        (w["case"],))]


def pending(w, today="2026-11-12"):
    return [r.opp_no for r in assign_list.pending_listed(w["conn"], today)]


def ask_on_site(w, day):
    w["repo"].add_note(w["case"], day, detect_followups.reply_note("Fenwick Cargo", QUESTION, kind="asks"),
                       source="web")
    w["repo"].commit()


def test_a_follow_up_logged_on_the_site_is_not_the_answer_when_its_mail_comes_in(world):
    w = world
    ask_on_site(w, "2026-11-06")
    w["repo"].add_followup(w["case"], "2026-11-06", channel="mail", source="web")
    w["repo"].commit()
    assert pending(w) == ["OPP-7501"]
    store(w, QUOTE, mail("2026-11-07 09:00", US, CHASE, "f1", internal=True))
    report = scan(w)
    assert report.followups == 0 and not hasattr(report, "answers")
    assert not any(n.startswith("Answered by") for n in notes(w))
    assert pending(w) == ["OPP-7501"]


def test_a_follow_up_logged_by_hand_that_day_is_not_the_answer_either(world):
    w = world
    # their question comes in by mail that morning: the scan notes it, nothing on Pending on us (FX4)
    store(w, QUOTE, mail("2026-11-06 09:00", AGENT, "Hi Tam,\n" + QUESTION + "\nNoor", "r1", to=(US,)))
    assert scan(w).replies == 1
    assert notes(w)[-1].startswith("Reply from Noor Allard (noted): ")
    assert pending(w) == []
    # she logs it as Waiting on us, and Log as sent, by hand
    w["repo"].add_note(w["case"], "2026-11-06", detect_followups.reply_note("Fenwick Cargo", QUESTION, kind="asks"),
                       source="manual")
    w["repo"].add_followup(w["case"], "2026-11-06")
    w["repo"].commit()
    assert pending(w) == ["OPP-7501"]
    # that afternoon's chaser comes into the archive
    store(w, mail("2026-11-06 16:00", US, CHASE, "f1", internal=True))
    report = scan(w)
    assert report.followups == 0 and not hasattr(report, "answers")
    assert not any(n.startswith("Answered by") for n in notes(w))
    assert pending(w) == ["OPP-7501"]


def test_a_mail_to_the_requester_that_is_not_a_follow_up_is_no_answer_any_more(world):
    w = world
    ask_on_site(w, "2026-11-06")
    w["repo"].add_followup(w["case"], "2026-11-06", channel="mail", source="web")
    w["repo"].commit()
    store(w, QUOTE, mail("2026-11-07 09:00", US, "Hi Noor,\nYes, the liftgate at Dalby is included.\nTam", "a1",
                         internal=True))
    report = scan(w)
    # FX4: only Answered (the case page or the team site) clears the question
    assert not hasattr(report, "answers")
    assert not any(n.startswith("Answered by") for n in notes(w))
    assert pending(w) == ["OPP-7501"]


def test_a_struck_out_follow_up_no_longer_counts_in_the_ladder(world):
    w = world
    event = w["repo"].add_followup(w["case"], "2026-11-06", channel="mail", note="FU detected: RE: x [mid:<v1@x>]",
                                   source="mail")
    w["repo"].commit()
    row = lambda: w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (w["case"],)).fetchone()  # noqa: E731
    assert tracking.ladder(w["conn"], row(), today="2026-11-12")["next"] == "Decide"
    event_id = event if isinstance(event, int) else w["conn"].execute(
        "SELECT MAX(id) FROM status_event WHERE kind = 'followup'").fetchone()[0]
    w["repo"].void_status_event(event_id)
    w["repo"].commit()
    assert tracking.ladder(w["conn"], row(), today="2026-11-12")["next"] == "FU1"
