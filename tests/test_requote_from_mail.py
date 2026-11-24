"""Batch RQ1: our revised quote on a Quoted case, read by Update from
Outlook, is offered as Re-quoted, not as a note - one test for 'our mail is
a revised quote' (detect_followups.revised_quote) shared with the morning
scan, one suggestion per mail whichever path saw it first, and a note
suggestion that is our own quote defaults to the quote it is.

Every company, person, place, date and number here is invented (OPP-8701
upward, .test domains).
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, mail_archive, suggestions
from opplist.outlook_bridge import MailInfo

OURS = "rowanpike@ourco.test"
THEIRS = "ines@marlowfreight.test"
SUBJECT = "RE: RFQ // MEM - SAI // 2 PLTS"


def mail(sent, body, sender=THEIRS, name="Ines Marlow", to=(OURS,), internal=False, mid=""):
    return MailInfo(subject=SUBJECT, received_on=sent[:10], received_at=sent, sent_at=sent,
                    entry_id="E-" + sent, conversation_id="CONV-8701", conversation_topic=SUBJECT[4:],
                    sender_name=name, sender_address=sender, sender_internal=internal,
                    to_addresses=list(to), cc_addresses=[], display_to="; ".join(to), body_excerpt=body,
                    folder_path="\\Inbox", message_id=mid or "<{}@x.test>".format(sent.replace(" ", "T")))


def ours(sent, body, mid=""):
    return mail(sent, body, sender=OURS, name="JJC-ROWAN PIKE", to=(THEIRS,), internal=True, mid=mid)


FIRST = ("Hi Ines,\n\nPlease see below rates for your reference.\nA/F USD 2.10/kg MIN USD 150\n\n"
         "From: Ines Marlow\nSent: Monday\n\nPlease quote")
REVISED = ("Hi Ines,\n\nHere is the trucking option again, dock to dock with a live unload this time.\n"
           "Trucking USD 1,480 per load\n\nFrom: Ines Marlow\nSent: Wednesday\n\nAny other option?")
CHASER = ("Hi Ines,\n\nJust checking back on Monday's figures, are they workable?\n"
          "A/F USD 2.10/kg MIN USD 150\n\nFrom: JJC-ROWAN PIKE\nSent: Monday\n\nPlease see below rates")


def thread(*extra):
    return [mail("2026-11-02 09:00", "Hi, please quote MEM - SAI, 2 plts."),
            ours("2026-11-03 15:00", FIRST, mid="<first@x.test>"),
            mail("2026-11-05 10:00", "Thanks Rowan, any other option by truck to MIA?", mid="<ask@x.test>")] \
        + list(extra)


class Search:
    """Update from Outlook's view of the conversation."""

    def __init__(self, mails):
        self.mails = mails

    def conversation_of(self, selected):
        return list(self.mails)

    def hydrate(self, mails):
        return list(mails)


def archive_of(tmp_path, mails):
    path = tmp_path / "mail.db"
    archive = mail_archive.open_archive(path)
    mail_archive.store(archive, [mail_archive.record_from_info(m, sent_at=m.sent_at + ":00") for m in mails])
    archive.commit()
    archive.close()
    return path


@pytest.fixture
def world(tmp_path):
    conn = db.open_db(tmp_path / "rq1.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["ourco.test"])
    marlow = add_party(repo, "Marlow Freight", country="Chile")
    repo.insert("party_domain", {"domain": "marlowfreight.test", "party_id": marlow})
    case = add_enquiry(repo, "OPP-8701", marlow, received="2026-11-02", conversation_id="CONV-8701",
                       source_subject=SUBJECT, origin_loc="MEM", origin_country="USA", dest_loc="SAI",
                       dest_country="Chile", service_mode="Truck")
    repo.set_status(case, "Quoted", "2026-11-03")
    repo.commit()
    yield {"conn": conn, "repo": repo, "case": case, "marlow": marlow, "tmp": tmp_path}
    conn.close()


def row(w, opp="OPP-8701"):
    return w["conn"].execute("SELECT * FROM v_enquiry WHERE opp_no = ?", (opp,)).fetchone()


def scan(w, mails):
    with mail_archive.ArchiveSearch(archive_of(w["tmp"], mails)) as search:
        return detect_followups.requoted_suggestions(w["conn"], search)


def rows(w, case=None):
    return w["conn"].execute("SELECT * FROM suggestion WHERE enquiry_id = ? ORDER BY id",
                             (case or w["case"],)).fetchall()


def test_update_from_outlook_on_a_quoted_case_offers_re_quoted_and_no_note(world):
    w = world
    revised = ours("2026-11-06 11:00", REVISED, mid="<revised@x.test>")
    made = suggestions.from_mail(w["repo"], row(w), revised, Search(thread(revised)))
    [s] = rows(w)
    assert made == [s["id"]]
    assert (s["kind"], s["proposed_status"], s["proposed_date"], s["note"], s["source"], s["message_id"]) == \
        ("status", "Quoted", "2026-11-06", detect_followups.REQUOTED_NOTE, "mail", "<revised@x.test>")
    assert "USD 1,480" in s["evidence_quote"]
    p = suggestions.plan(w["conn"], s)
    assert (p.action, p.label, p.at) == ("requote", "Re-quoted · round 2", "2026-11-06")
    suggestions.accept(w["repo"], s["id"])
    event = w["conn"].execute("SELECT at, status, kind FROM status_event WHERE enquiry_id = ? ORDER BY id DESC",
                              (w["case"],)).fetchone()
    assert tuple(event) == ("2026-11-06", "Quoted", "requote")


def test_the_morning_scan_then_adds_none_for_that_mail(world):
    w = world
    revised = ours("2026-11-06 11:00", REVISED, mid="<revised@x.test>")
    assert len(scan(w, thread(revised))) == 1                       # before: the scan would offer it
    suggestions.from_mail(w["repo"], row(w), revised, Search(thread(revised)))
    assert scan(w, thread(revised)) == []
    assert len(rows(w)) == 1


def test_a_mail_the_scan_offered_is_not_offered_again_by_update_from_outlook(world):
    w = world
    revised = ours("2026-11-06 11:00", REVISED, mid="<revised@x.test>")
    detect_followups.record_requoted(db.Repository(w["conn"], source="scan"), scan(w, thread(revised)))
    [s] = rows(w)
    suggestions.dismiss(w["repo"], s["id"])
    # dismissed by the other path: neither a re-quote nor a note
    assert suggestions.from_mail(w["repo"], row(w), revised, Search(thread(revised))) == []
    assert [r["id"] for r in rows(w)] == [s["id"]]


def test_a_dismissed_re_quote_from_update_from_outlook_is_not_offered_by_the_scan(world):
    w = world
    revised = ours("2026-11-06 11:00", REVISED, mid="<revised@x.test>")
    [sid] = suggestions.from_mail(w["repo"], row(w), revised, Search(thread(revised)))
    suggestions.dismiss(w["repo"], sid)
    assert scan(w, thread(revised)) == []


def test_a_chaser_repeating_the_old_rate_is_not_a_re_quote(world):
    w = world
    chaser = ours("2026-11-06 11:00", CHASER, mid="<chaser@x.test>")
    assert scan(w, thread(chaser)) == []
    suggestions.from_mail(w["repo"], row(w), chaser, Search(thread(chaser)))
    [s] = rows(w)
    assert s["kind"] == "note"
    assert suggestions.plan(w["conn"], s).action == "note"
    assert detect_followups.revised_quote(
        chaser, row(w), {"marlowfreight.test"}, lambda m: True, "2026-11-03") == ""


def test_the_shared_test_reads_a_quote_phrase_without_an_amount_and_not_a_request_to_a_vendor(world):
    w = world
    domains = {"marlowfreight.test"}
    phrase = ours("2026-11-06 11:00", "Hi Ines,\n\nPlease find attached our revised quotation.\n\nRowan")
    assert detect_followups.revised_quote(phrase, row(w), domains, lambda m: True, "2026-11-03") \
        == "Please find attached our revised quotation."
    asking = ours("2026-11-06 11:00", "Hi Ines,\n\nPlease kindly provide your rates as per below.\n\nRowan")
    assert detect_followups.revised_quote(asking, row(w), domains, lambda m: True, "2026-11-03") == ""
    # the day of the last quote is that quote
    same_day = ours("2026-11-03 17:00", REVISED)
    assert detect_followups.revised_quote(same_day, row(w), domains, lambda m: True, "2026-11-03") == ""


def note_row(w, case, day, words="Please see below rates for your reference.",
             sender="JJC-ROWAN PIKE <{}>".format(OURS), opp="OPP-8702"):
    sid = suggestions.record(w["repo"], case, opp, {
        "kind": "note", "note": words, "proposed_date": day, "evidence_sender": sender,
        "evidence_date": day, "evidence_quote": words, "confidence": "low", "entry_id": "E-" + day,
        "source": "mail"})
    w["repo"].commit()
    return w["conn"].execute("SELECT * FROM suggestion WHERE id = ?", (sid,)).fetchone()


def test_a_note_with_our_quote_words_defaults_to_quoted_on_a_new_case(world):
    w = world
    case = add_enquiry(w["repo"], "OPP-8702", w["marlow"], received="2026-11-02")
    s = note_row(w, case, "2026-11-04")
    p = suggestions.plan(w["conn"], s)
    assert (p.action, p.label) == ("quoted", "Quoted")
    assert ("note", "Note only", False) in suggestions.options(w["conn"], s)
    # their words, or ours without a quote, stay a note
    assert suggestions.plan(w["conn"], note_row(
        w, case, "2026-11-05", sender="Ines Marlow <{}>".format(THEIRS))).action == "note"
    assert suggestions.plan(w["conn"], note_row(
        w, case, "2026-11-06", words="We are still waiting for the trucker.")).action == "note"


def test_a_note_with_our_quote_words_on_a_quoted_case_defaults_to_re_quoted(world):
    w = world
    s = note_row(w, w["case"], "2026-11-06", opp="OPP-8701")
    assert suggestions.plan(w["conn"], s).label == "Re-quoted · round 2"


def test_a_note_with_our_quote_words_on_a_lost_case_before_its_closing_is_history(world):
    w = world
    case = add_enquiry(w["repo"], "OPP-8703", w["marlow"], received="2026-11-02")
    w["repo"].set_status(case, "Lost", "2026-11-12", closed_reason="Price too high")
    w["repo"].commit()
    s = note_row(w, case, "2026-11-04", opp="OPP-8703")
    p = suggestions.plan(w["conn"], s)
    assert (p.action, p.label) == ("history_quoted", "Quoted, history before Lost Nov 12")
    opts = suggestions.options(w["conn"], s)
    assert opts[0] == ("history_quoted", "Quoted, history before Lost Nov 12", True)
    assert ("note", "Note only", False) in opts
    suggestions.accept(w["repo"], s["id"])
    assert w["conn"].execute("SELECT status FROM enquiry WHERE id = ?", (case,)).fetchone()[0] == "Lost"
    assert w["conn"].execute("SELECT decided_as FROM suggestion WHERE id = ?", (s["id"],)).fetchone()[0] == "history"
    # after the closing it stays a note
    assert suggestions.plan(w["conn"], note_row(w, case, "2026-11-14", opp="OPP-8703")).action == "note"


def test_accepted_quote_notes_without_a_quote_that_day_are_listed(world):
    w = world
    case = add_enquiry(w["repo"], "OPP-8704", w["marlow"], received="2026-11-02", status="Quoted")
    w["repo"].commit()
    s = note_row(w, case, "2026-11-07", opp="OPP-8704")
    suggestions.decide(w["repo"], s, {"action": "note", "at": "2026-11-07", "note": s["note"]})
    listed = suggestions.quote_notes_accepted(w["conn"])
    assert [(o, d) for o, d, *_ in listed] == [("OPP-8704", "2026-11-07")]
    # one with a requote that day is not
    w["repo"].requote(case, "2026-11-07", source="manual")
    w["repo"].commit()
    assert suggestions.quote_notes_accepted(w["conn"]) == []
