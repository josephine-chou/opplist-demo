"""Batch T4: a mail we send to a vendor is not an answer to the requester.
The line after which the requester's mails are replies is our last mail to
the requester (else the quote day); a follow-up is read only from a mail to
the requester; one of our offices asked as a vendor is never writing to the
requester. Also the invented replacements of the old follow-up tests that
put our mails on an internal To line only.

Every company, person, mail, place and date here is invented; cases from
OPP-7401. FX4 (2026-11-12): the scan writes every reply as noted, reads no
answer off our mail, and latest_reply went with the draft it held; the
reply line is checked on what the scan writes.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import assign_list, db, detect_followups, mail_archive, parsing, suggestions, tracking
from opplist.outlook_bridge import MailInfo

SUBJECT = "RATE REQUEST // MARLOW - TESSEL // 1 X 40HQ"
NUMBERED = "[OPP-7401] RATE REQUEST // Marlow - Tessel // Ocean // FOB"
AGENT = ("odile@brackenfield.test", "Odile Marsh")
US = ("pia@jojofreight.example", "JJC-PIA ROWE")
TEAM = ("JJC-MFN@jojofreight.example", "#JJC-MFN")
VENDOR = ("rates@quillonfreight.test", "Quillon Rates")
OFFICE = ("hugo@jojofreight.example", "ZQX-HUGO LAMB")          # one of our offices asked as a vendor


def mail(at, sender, body, mid, internal=False, to=(US,), subject="RE: " + SUBJECT, conversation="CONV-7401"):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, sent_at=at, entry_id="EID-" + mid,
                    conversation_id=conversation, conversation_topic=parsing.clean_subject(subject),
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=[a for a, _ in to], body_excerpt=body, folder_path="\\Mailbox\\Inbox",
                    display_to="; ".join(n for _, n in to), message_id="<{}@x>".format(mid))


def to_vendor(at, body, mid, sender=US, to=(VENDOR, TEAM)):
    return mail(at, sender, body, mid, internal=True, to=to, subject=NUMBERED, conversation="CONV-V-" + mid)


QUOTE = mail("2026-11-03 10:00", US, "Hi Odile,\nOur rate is USD 2,640 per 40HQ all-in.\nRegards", "q1",
             internal=True, to=(AGENT,))
ASKS = mail("2026-11-05 09:00", AGENT, "Hi Pia,\nCould you also quote LCL for 4 pallets?\nOdile", "r1")
NOTED = mail("2026-11-05 09:00", AGENT, "Hi Pia,\nThanks, still checking with the shipper.\nOdile", "r1")
CHASER = "Hi,\nAny update on this one?\nRegards"


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    conn = db.open_db(tmp_path / "t4.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    agent = add_party(repo, "Brackenfield Cargo", country="Chile")
    repo.insert("party_domain", {"domain": "brackenfield.test", "party_id": agent})
    case = add_enquiry(repo, "OPP-7401", agent, received="2026-11-02", source_subject=SUBJECT,
                       thread_key=parsing.subject_thread_key(SUBJECT), entry_id="EID-i1",
                       conversation_id="CONV-7401", service_mode="FCL", origin_country="USA",
                       dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-11-03")
    # the vendor asked on the case: its numbered mails are the case's (case_numbers.keep)
    vendor = add_party(repo, "Quillon Freight", country="Chile")
    repo.insert("party_domain", {"domain": "quillonfreight.test", "party_id": vendor})
    repo.insert("vendor_request", {"enquiry_id": case, "vendor_party_id": vendor, "asked_on": "2026-11-02"})
    repo.commit()
    yield {"conn": conn, "repo": repo, "case": case, "archive": archive, "path": tmp_path / "t4.db"}
    conn.close()


def store(w, *mails):
    conn = mail_archive.open_archive(w["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def scan(w, dry_run=False):
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        return detect_followups.run(w["conn"], search, dry_run=dry_run)


def row(w):
    return w["conn"].execute("SELECT * FROM v_enquiry WHERE id = ?", (w["case"],)).fetchone()


def in_thread(w, *mids):
    """The numbered mails really are on the case's thread."""
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        found = {m.message_id for m in detect_followups.thread_of(search, row(w), w["conn"])}
    return all("<{}@x>".format(mid) in found for mid in mids)


def events(w, kind):
    return [tuple(r) for r in w["conn"].execute(
        "SELECT at, note FROM status_event WHERE enquiry_id = ? AND kind = ? AND voided = 0 ORDER BY at, id",
        (w["case"], kind))]


def accept_found(w):
    """FX7: the follow-ups the scan found wait in Suggested updates; Accept
    writes each as the scan used to."""
    for row in w["conn"].execute("SELECT id FROM suggestion WHERE kind = 'followup' AND state = 'pending' "
                                 "ORDER BY id").fetchall():
        suggestions.accept(db.Repository(w["conn"], source="test"), row["id"])


def step(w, today="2026-11-20"):
    for item in tracking.rule_quoted_no_answer(w["conn"], dt.date.fromisoformat(today)):
        if item.enquiry_id == w["case"]:
            return item.extra["step"]
    return None


# ------------------------------------------------------- the reply line --
def test_their_question_before_our_mail_to_a_vendor_is_a_reply(world):
    w = world
    store(w, QUOTE, ASKS, to_vendor("2026-11-05 11:00", "Hi,\nPlease quote LCL 4 pallets Marlow - Tessel.\nPia", "v1"))
    assert in_thread(w, "v1")
    report = scan(w)
    assert report.replies == 1
    # FX4: written as noted, though the words ask; nothing on Pending on us
    note = events(w, "note")[-1][1]
    assert note.startswith("Reply from Odile Marsh (noted): Hi Pia, Could you also quote LCL")
    assert detect_followups.reply_kind(note.split(": ", 1)[1]) == "asks"
    assert step(w) != "Replied"
    assert not [r for r in assign_list.pending_listed(w["conn"], "2026-11-20") if r.opp_no == "OPP-7401"]


def test_our_mail_to_the_requester_after_their_reply_still_moves_the_line(world):
    w = world
    store(w, QUOTE, ASKS, mail("2026-11-06 10:00", US, "Hi Odile,\nLCL is USD 95 per cbm.\nPia", "a1",
                               internal=True, to=(AGENT,)))
    report = scan(w)
    assert report.replies == 0 and events(w, "note") == []


def test_with_no_mail_to_the_requester_the_line_is_the_quote_day(world):
    w = world
    store(w, mail("2026-11-02 18:00", AGENT, "Hi,\nIs the rate for 1 x 40HQ?\nOdile", "r0"),
          ASKS, to_vendor("2026-11-06 08:00", "Hi,\nPlease quote.\nPia", "v1"))
    report = scan(w)
    assert report.replies == 1                                    # the 5 Nov one, not the one before the quote
    assert events(w, "note")[-1][0] == "2026-11-05"


# -------------------------------------------------------------- follow-ups --
def test_any_update_to_a_vendor_is_not_a_follow_up(world):
    w = world
    store(w, QUOTE, to_vendor("2026-11-07 10:00", CHASER, "v1"))
    assert in_thread(w, "v1")
    report = scan(w)
    assert report.followups == 0 and events(w, "followup") == []


def test_the_same_words_to_the_requester_are_a_follow_up(world):
    w = world
    store(w, QUOTE, mail("2026-11-07 10:00", US, CHASER, "f1", internal=True, to=(AGENT,)))
    report = scan(w)
    assert report.followups == 1 and events(w, "followup") == []          # FX7: a suggestion first
    accept_found(w)
    assert events(w, "followup") == [("2026-11-07", "FU detected: RE: {} [mid:<f1@x>]".format(SUBJECT))]


def test_a_follow_up_logged_by_hand_that_day_is_not_doubled(world):
    w = world
    store(w, QUOTE, mail("2026-11-07 10:00", US, CHASER, "f1", internal=True, to=(AGENT,)))
    w["repo"].add_followup(w["case"], "2026-11-07")
    w["conn"].commit()
    report = scan(w)
    assert (report.followups, report.skipped) == (0, 1)
    assert len(events(w, "followup")) == 1


def test_dry_run_writes_nothing(world):
    w = world
    store(w, QUOTE, mail("2026-11-07 10:00", US, CHASER, "f1", internal=True, to=(AGENT,)))
    report = scan(w, dry_run=True)
    assert report.followups == 1 and "dry run: nothing was written" in report.summary()
    assert events(w, "followup") == []


def test_the_command_runs_and_dry_runs(world, capsys):
    from opplist.__main__ import main

    w = world
    store(w, QUOTE, mail("2026-11-07 10:00", US, CHASER, "f1", internal=True, to=(AGENT,)))
    assert main(["detect-followups", "--db", str(w["path"]), "--archive", str(w["archive"]), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "follow-ups found      : 1" in out and "dry run" in out
    assert main(["detect-followups", "--db", str(w["path"]), "--archive", str(w["archive"])]) == 0
    assert "follow-ups suggested  : 1" in capsys.readouterr().out
    accept_found(w)
    assert len(events(w, "followup")) == 1


def test_the_thread_is_found_by_topic_when_there_is_no_conversation_id(world):
    w = world
    w["conn"].execute("UPDATE enquiry SET conversation_id = NULL WHERE id = ?", (w["case"],))
    w["conn"].commit()
    store(w, mail("2026-11-07 10:00", US, CHASER, "f1", internal=True, to=(AGENT,), conversation="OTHER"))
    assert scan(w).followups == 1


# ------------------------------------------- one of our offices as a vendor --
def test_an_office_asked_as_a_vendor_never_writes_to_the_requester(world):
    w = world
    answer = mail("2026-11-06 10:00", OFFICE, "Hi Pia,\nAny update? Our rate is USD 1,900 per 40HQ.\nHugo",
                  "o1", internal=True, to=(US, AGENT), subject="RE: " + NUMBERED, conversation="CONV-O1")
    store(w, QUOTE, to_vendor("2026-11-04 09:00", "Hi Hugo,\nPlease quote.\nPia", "v1", to=(OFFICE,)),
          answer, mail("2026-11-07 09:00", AGENT, "Hi Pia,\nDoes the rate include THC?\nOdile", "r2"))
    assert in_thread(w, "v1", "o1")
    assert detect_followups._to_requester(answer, row(w), {"brackenfield.test"}) is False
    report = scan(w)
    assert report.followups == 0
    assert report.replies == 1 and events(w, "note")[-1][0] == "2026-11-07"
    assert events(w, "note")[-1][1].startswith("Reply from Odile Marsh (noted): Hi Pia, Does the rate include THC?")


# ------------------------------- the reply before our mail to a vendor (was: the draft warning) --
def test_a_reply_that_came_before_our_mail_to_a_vendor_is_read(world):
    w = world
    store(w, QUOTE, NOTED, to_vendor("2026-11-06 10:00", "Hi,\nPlease quote.\nPia", "v1"))
    assert in_thread(w, "v1")
    assert scan(w).replies == 1
    (at, note), = events(w, "note")
    assert at == "2026-11-05" and note.startswith("Reply from Odile Marsh (noted): ")


# --------------------------------------------- one of our offices requester --
def test_a_own_office_requester_works_as_before(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    conn = db.open_db(tmp_path / "office.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    office = add_party(repo, "JoJo QVR", ptype="Office", country="Chile")
    subject = "DAP from Marlow to Tessel"
    case = add_enquiry(repo, "OPP-7402", office, received="2026-11-02", conversation_id="CONV-7402",
                       source_subject=subject)
    repo.set_status(case, "Quoted", "2026-11-03")
    repo.commit()
    mei = ("ines@jojofreight.example", "QVR-INES BALD")
    w = {"archive": archive}
    store(w, mail("2026-11-03 10:00", US, "Hi Ines,\nOur rate is USD 2,100 per 40HQ.\nPia", "oq",
                  internal=True, to=(mei,), subject="RE: " + subject, conversation="CONV-7402"),
          mail("2026-11-07 10:00", US, CHASER, "of", internal=True, to=(mei,), subject="RE: " + subject,
               conversation="CONV-7402"),
          mail("2026-11-09 10:00", mei, "Hi Pia,\nCan you hold the space?\nInes", "or", internal=True,
               to=(US,), subject="RE: " + subject, conversation="CONV-7402"))
    with mail_archive.ArchiveSearch(archive) as search:
        report = detect_followups.run(conn, search, opp_nos=["OPP-7402"])
    assert (report.followups, report.replies) == (1, 1)
    note = conn.execute("SELECT note FROM status_event WHERE enquiry_id = ? AND note LIKE 'Reply from%'",
                        (case,)).fetchone()[0]
    assert note.startswith("Reply from QVR-INES BALD (noted): ")
    conn.close()


# ----------------------------------- FX4: no AL6a; only Answered clears a question --
def test_neither_our_mail_to_the_requester_nor_one_to_a_vendor_answers_a_question(world):
    w = world
    store(w, QUOTE, ASKS, to_vendor("2026-11-05 11:00", "Hi,\nPlease quote LCL.\nPia", "v1"))
    assert in_thread(w, "v1")
    scan(w)
    assert step(w) != "Replied"                                     # the scan's note never asks
    # she logs the question by hand (Waiting on us, as the case page writes it)
    w["repo"].add_note(w["case"], "2026-11-05", detect_followups.reply_note("Brackenfield Cargo", "LCL too?",
                                                                            kind="asks"), source="manual")
    w["repo"].commit()
    assert step(w) == "Replied"
    store(w, mail("2026-11-06 10:00", US, "Hi Odile,\nLCL is USD 95 per cbm.\nPia", "a1",
                  internal=True, to=(AGENT,)))
    report = scan(w)
    assert not hasattr(report, "answers")
    assert not any(n.startswith("Answered by") for _, n in events(w, "note"))
    assert step(w) == "Replied"                                     # Answered clears it, nothing else


def test_a_noted_reply_after_our_follow_up_moves_the_clock(world):
    w = world
    store(w, QUOTE, mail("2026-11-05 10:00", US, CHASER, "f1", internal=True, to=(AGENT,)),
          mail("2026-11-09 11:00", AGENT, "Hi Pia,\nStill checking with the shipper, will revert.\nOdile", "r3"))
    report = scan(w)
    assert (report.followups, report.replies) == (1, 1)
    accept_found(w)                                                       # FX7
    assert events(w, "note")[-1][1].startswith("Reply from Odile Marsh (noted): ")
    assert {s.rule: s for s in tracking.run(w["conn"], today="2026-11-12")}["R3"].items == []
    item = {s.rule: s for s in tracking.run(w["conn"], today="2026-11-16")}["R3"].items[0]
    assert (item.extra["step"], item.since, item.days) == ("FU2", "2026-11-09", 5)
    again = scan(w)
    assert (again.followups, again.replies, again.skipped) == (0, 0, 2)


def test_the_office_handling_the_case_writes_for_us(world):
    w = world
    w["conn"].execute("UPDATE enquiry SET handled_by = 'ZQX' WHERE id = ?", (w["case"],))
    w["conn"].commit()
    quote = mail("2026-11-03 18:00", OFFICE, "Hi Odile,\nOur rate is USD 2,580 per 40HQ.\nHugo", "hq",
                 internal=True, to=(AGENT,))
    store(w, mail("2026-11-03 09:00", AGENT, "Hi Hugo,\nPlease quote 1 x 40HQ.\nOdile", "i0"), quote)
    assert detect_followups._to_requester(quote, row(w), {"brackenfield.test"}) is True
    assert scan(w).replies == 0 and events(w, "note") == []
