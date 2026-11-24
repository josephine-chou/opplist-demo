"""Batch AL6a: the requester's replies are read by their own words (the
quote cut at Chinese and Vietnamese headers, links taken out, more ways of
asking), our answer to a reply that asked us something ends Replied, and
--reclassify --from-archive reads the marks again from the mails. FX4
(2026-11-12): the scan writes every reply as noted and never reads our mail
as the answer; only Answered clears a question logged by hand.

Every company, person, mail, place and date here is invented; cases from
OPP-6101.
"""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, mail_archive, parsing, tracking
from opplist.outlook_bridge import MailInfo

SUBJECT = "RATE REQUEST // SAVANNAH - IQUIQUE // 2 X 40HQ"
AGENT = ("teodora@saltmarshlog.example", "Teodora Vance")
US = ("desk@jojofreight.example", "JJC-DESK")
MATE = ("ravi@jojofreight.example", "JJC-RAVI OKON")


# ------------------------------------------------------- A. their own words --
@pytest.mark.parametrize("header", [
    "发件人： JJZ-MEI LAN <mei@jojofreight.example>",                 # 发件人： full-width colon
    "-------- 原始邮件 --------",                              # -------- 原始邮件 --------
    "原始郵件",                                                # 原始郵件, no dashes
    "寄件者：JJC-DESK",                                         # 寄件者：
    "Vào Thứ 5, 17 thg 9, 2026 vào lúc 05:58 JJC-DESK <desk@jojofreight.example> đã viết:",
])
def test_the_quote_is_cut_at_the_new_headers(header):
    body = "Hi team,\nThe cargo is ready next week.\nTeodora\n\n" + header + "\nCould you send the rate?\n"
    assert mail_archive.top_part(body) == "Hi team,\nThe cargo is ready next week.\nTeodora"


def test_a_question_mark_in_a_link_is_not_a_question():
    for text in ("Thanks, noted. https://saltmarshlog.example/track?id=77",
                 "Thanks. <https://saltmarshlog.example/about?x=1> www.saltmarshlog.example/?ref=sig",
                 "Thanks, noted. <mailto:teodora@saltmarshlog.example?subject=hi>"):
        assert detect_followups.reply_kind(text) == "noted", text
    assert detect_followups.reply_kind("Noted. Is the rate all-in?") == "asks"


@pytest.mark.parametrize("text", [
    "Please also provide the MAWB consignee details.",
    "Kindly review the attached packing list.",
    "Please clarify the free time at destination.",
    "Kindly update the rate for the new date.",
    "Please revise the quote for 3 units.",
    "We would like to obtain the rate for LCL as well.",
    "We'd like to also know the transit time.",
    "We’d like to request a booking.",
    "I would also like to have each carrier's charges listed apart.",            # AL6: also before 'like to'
    "I'd like to know the cut-off.",
    "Please we need your help on this one.",
    "We need your best rates for this lane.",
    "We need your confirmation by Friday.",
])
def test_each_new_phrase_asks(text):
    assert detect_followups.reply_kind(text) == "asks"


def test_let_us_know_as_a_courtesy_is_noted():
    assert detect_followups.reply_kind("Thank you. Let us know should anything be missing.") == "noted"


# --------------------------------------------------- the world and its mails --
def mail(at, sender, body, mid, internal=False, to=(US[0],), subject="RE: " + SUBJECT):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, entry_id="EID-" + mid,
                    conversation_id="CONV-6101", conversation_topic=parsing.clean_subject(subject),
                    sender_name=sender[1], sender_address=sender[0], sender_internal=internal,
                    to_addresses=list(to), body_excerpt=body, folder_path="\\Mailbox\\Inbox",
                    display_to="; ".join(to), message_id="<{}@x>".format(mid))


INQUIRY = mail("2026-11-02 09:00", AGENT, "Hi,\nPlease quote 2 x 40HQ Savannah - Iquique.\nTeodora", "i1",
               subject=SUBJECT)
QUOTE = mail("2026-11-03 10:00", US, "Hi Teodora,\nOur rate is USD 2,950 per 40HQ all-in.\nRegards", "q1",
             internal=True, to=(AGENT[0],))
ASKING = mail("2026-11-05 09:00", AGENT, "Hi,\nCould you also quote LCL for 6 pallets?\nTeodora", "r1")


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    conn = db.open_db(tmp_path / "al6a.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    salt = add_party(repo, "Saltmarsh Logistics", country="Chile")
    repo.insert("party_domain", {"domain": "saltmarshlog.example", "party_id": salt})
    case = add_enquiry(repo, "OPP-6101", salt, received="2026-11-02", source_subject=SUBJECT,
                       thread_key=parsing.subject_thread_key(SUBJECT), entry_id="EID-i1",
                       conversation_id="CONV-6101", service_mode="FCL", origin_country="USA", dest_country="Chile")
    repo.set_status(case, "Quoted", "2026-11-03")
    repo.commit()
    yield {"conn": conn, "repo": repo, "case": case, "archive": archive}
    conn.close()


def store(w, *mails):
    conn = mail_archive.open_archive(w["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def scan(w, dry_run=False):
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        return detect_followups.run(w["conn"], search, dry_run=dry_run)


def notes(w):
    return [r[0] for r in w["conn"].execute(
        "SELECT note FROM status_event WHERE enquiry_id = ? AND kind = 'note' AND voided = 0 ORDER BY at, id",
        (w["case"],))]


def step(w, today="2026-11-20"):
    for item in tracking.rule_quoted_no_answer(w["conn"], dt.date.fromisoformat(today)):
        if item.enquiry_id == w["case"]:
            return item.extra["step"]
    return None


def test_a_question_only_in_the_part_quoted_under_a_chinese_header_reads_as_noted(world):
    w = world
    progress = mail("2026-11-05 09:00", AGENT, "Hi there,\n1. The container is on hold.\n2. POD unchanged.\nTeodora\n\n"
                    "发件人： JJC-DESK <desk@jojofreight.example>\nAny update on the rates? Could you advise?",
                    "p1")
    store(w, INQUIRY, QUOTE, progress)
    scan(w)
    assert notes(w)[-1].startswith("Reply from Teodora Vance (noted): Hi there, 1. The container is on hold.")
    assert "Any update" not in notes(w)[-1]


# ------------------------------- C. FX4: no Replied from the scan, no answer read off our mail --
def test_their_question_is_noted_and_our_answer_is_not_read_as_one(world):
    w = world
    store(w, INQUIRY, QUOTE, ASKING)
    scan(w)
    assert notes(w)[-1].startswith("Reply from Teodora Vance (noted):")
    assert detect_followups.reply_kind(notes(w)[-1].split(": ", 1)[1]) == "asks"     # the words still ask
    assert step(w) != "Replied"                                    # FX4: the scan never puts it there
    # to a colleague, then our mail to them
    store(w, mail("2026-11-05 17:00", US, "Ravi, can you check LCL Savannah - Iquique?", "c1", internal=True,
                  to=(MATE[0],)),
          mail("2026-11-06 11:00", US, "Hi Teodora,\nLCL for 6 pallets: we will revert by tomorrow.\nRegards", "a1",
               internal=True, to=(AGENT[0],)))
    report = scan(w, dry_run=True)
    assert not hasattr(report, "answers") and [l for l in report.lines if " answered " in l] == []
    scan(w)
    assert not any(n.startswith("Answered by") for n in notes(w))
    assert step(w) != "Replied"


def test_a_mail_before_their_reply_or_a_follow_up_is_not_an_answer(world):
    w = world
    store(w, INQUIRY, QUOTE, ASKING)
    scan(w)
    # a chaser of ours after their question is recorded by step 1 as a follow-up, never as an answer
    store(w, mail("2026-11-07 09:00", US, "Hi Teodora,\nJust checking back on our quote.\nThank you.", "f1",
                  internal=True, to=(AGENT[0],)))
    report = scan(w)
    assert report.followups == 1 and not hasattr(report, "answers")
    assert not any(n.startswith("Answered by") for n in notes(w))


def test_a_case_that_is_not_replied_gets_no_answer(world):
    w = world
    thanks = mail("2026-11-05 09:00", AGENT, "Hi,\nThanks, noted. I will check with my client.\nTeodora", "t1")
    store(w, INQUIRY, QUOTE, thanks)
    scan(w)
    store(w, mail("2026-11-06 11:00", US, "Hi Teodora,\nSure, take your time.\nRegards", "a2", internal=True,
                  to=(AGENT[0],)))
    report = scan(w)
    assert not hasattr(report, "answers") and not any(n.startswith("Answered by") for n in notes(w))


# ------------------------------------------------ D. reading the marks again --
def test_reclassify_from_the_archive_changes_only_marks_and_keeps_what_it_cannot_read(world):
    w = world
    repo, conn = w["repo"], w["conn"]
    # an old note, marked by its 120 characters: their question was in the
    # part quoted under the Chinese header, which the old reading kept
    wrong = mail("2026-11-05 09:00", AGENT, "Hi there,\nThe container is on hold.\nTeodora\n\n"
                 "发件人： JJC-DESK\nCould you advise?", "w1")
    store(w, INQUIRY, QUOTE, wrong)
    repo.add_note(w["case"], "2026-11-05", "Reply from Teodora Vance (needs an answer): Hi there, The container "
                  "is on hold. Teodora [mid:<w1@x>]", source="mail")
    repo.add_note(w["case"], "2026-11-04", "Reply from Teodora Vance (noted): Thanks. [mid:<gone@x>]", source="mail")
    repo.add_note(w["case"], "2026-11-04", "Reply from Teodora Vance (needs an answer): No id here?", source="mail")
    repo.commit()
    before = notes(w)
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        dry = detect_followups.reclassify(conn, dry_run=True, search=search)
        assert notes(w) == before and dry.rewritten == 1 and dry.kept == 2
        assert dry.lines == ["OPP-6101 2026-11-05 Hi there, The container is on hold. Teodora [mid:<w1@x>] -> (noted)"]
        detect_followups.reclassify(conn, dry_run=False, search=search)
    after = notes(w)
    assert after[-1] == before[-1].replace("(needs an answer)", "(noted)")      # only the mark
    assert after[:-1] == before[:-1]                                         # the unreadable ones kept


# ------------------------------------------------------ the body_top recompute --
def test_the_body_top_recompute_changes_only_the_mails_whose_top_part_changes(world):
    w = world
    old_cut = mail("2026-11-05 09:00", AGENT, "Hi,\nOn hold.\n\n发件人： JJC-DESK\nCould you advise?", "b1")
    store(w, INQUIRY, QUOTE, old_cut)
    conn = mail_archive.open_archive(w["archive"])
    conn.execute("UPDATE message SET body_top = body WHERE message_id = '<b1@x>'")          # as synced before AL6a
    conn.commit()
    tops = dict(conn.execute("SELECT message_id, body_top FROM message").fetchall())
    assert mail_archive.recompute_body_top(conn, dry_run=True) == (1, 3)
    assert mail_archive.recompute_body_top(conn) == (1, 3)
    now = dict(conn.execute("SELECT message_id, body_top FROM message").fetchall())
    assert now["<b1@x>"] == "Hi,\nOn hold." and {k: v for k, v in now.items() if k != "<b1@x>"} == \
        {k: v for k, v in tops.items() if k != "<b1@x>"}
    assert mail_archive.recompute_body_top(conn) == (0, 3)
    conn.close()
