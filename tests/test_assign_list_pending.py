"""Batch AL6: the Assign list's Pending on us - Tracking's Replied rows -
and What They Asked. FX4 (2026-11-12): only questions logged by hand (the
case page, source manual, or the team site, source web) put a case here;
the scan's replies (source mail) never do. The Google Sheet's sync went in CX1 (2026-11-04); the
list is the team site's (assign_list). Every company, person, mail and
date here is invented; cases from OPP-6201.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import assign_list, db, mail_archive, tracking
from opplist.outlook_bridge import MailInfo

TODAY = "2026-11-20"


def asks(who, words, mid):
    return "Reply from {} (needs an answer): {} [mid:<{}@x>]".format(who, words, mid)


@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    path = tmp_path / "pending.db"
    conn = db.open_db(path)
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "owners", ["Ana", "Ben", "Cara"])
    heron = add_party(repo, "Heronsgate Cargo", country="Chile")
    marsh = add_party(repo, "Marshfield Mills", ptype="Direct Customer", country="USA")
    ids = {}

    def quoted(opp, reply_on=None, note=None, party=heron, source="manual", **extra):
        case = add_enquiry(repo, opp, party, received="2026-11-02", **extra)
        repo.set_status(case, "Quoted", "2026-11-03")
        if note:
            repo.add_note(case, reply_on, note, source=source)
        ids[opp] = case
        return case

    quoted("OPP-6201", "2026-11-10", asks("Ines Rook", "Could you add the THC at destination?", "r6201"))
    quoted("OPP-6202", "2026-11-06", asks("Ines Rook", "Can you quote LCL too?", "r6202"), owner="Ben")
    quoted("OPP-6203", "2026-11-12", "Reply from Ines Rook (noted): Thanks, will check with my client. [mid:<n@x>]")
    quoted("OPP-6204")                                                                  # FU1 due, no reply
    quoted("OPP-6205", "2026-11-09", asks("Pell Ward", "What is the transit time?", "r6205"), party=marsh)
    quoted("OPP-6206", "2026-11-09", asks("Ines Rook", "Please advise free time?", "r6206"), handled_by="YUL")
    quoted("OPP-6207", "2026-11-08", asks("Ines Rook", "Can you confirm the cut-off?", "r6207"))
    tracking.snooze(repo, "R3", "enquiry", ids["OPP-6207"], "2026-11-25")
    quoted("OPP-6208", "2026-11-07", asks("Ines Rook", "Any chance of a better rate?", "r6208"))
    # held after it was quoted (a status change ends a hold)
    repo.update("enquiry", ids["OPP-6208"], {"hold_reason": "waiting for the client", "hold_until": "2026-11-28"})
    quoted("OPP-6209", "2026-11-05", asks("Ines Rook", "Could you send the schedule?", "r6209"))
    repo.add_note(ids["OPP-6209"], "2026-11-06", "Answered by JJC-DESK: Schedule attached.", source="manual")
    # FX4: a question the scan wrote (before FX4 it was marked as asking) is not on the list
    quoted("OPP-6210", "2026-11-11", asks("Ines Rook", "Can you add insurance?", "r6210"), source="mail")
    repo.commit()
    yield {"conn": conn, "repo": repo, "path": path, "ids": ids, "heron": heron, "archive": archive,
           "tmp": tmp_path}
    conn.close()


def store(w, *mails):
    conn = mail_archive.open_archive(w["archive"])
    mail_archive.store(conn, [mail_archive.record_from_info(m) for m in mails])
    conn.commit()
    conn.close()


def mail(mid, body, sender=("ines@heronsgate.example", "Ines Rook")):
    return MailInfo(subject="RE: RATE REQUEST", received_on="2026-11-10", received_at="2026-11-10 09:00",
                    entry_id="EID-" + mid, conversation_id="CONV-" + mid, conversation_topic="RATE REQUEST",
                    sender_name=sender[1], sender_address=sender[0], body_excerpt=body,
                    folder_path="\\Mailbox\\Inbox", message_id="<{}@x>".format(mid))


# ------------------------------------------------------------- A. the list --
def test_which_cases_are_pending_on_us_and_in_what_order(world):
    w = world
    rows = assign_list.pending_listed(w["conn"], TODAY)
    # not assigned first, oldest Asked On first; then the assigned
    # D7c (her choice A): a direct customer's question is here too (OPP-6205)
    assert [(r.opp_no, r.asked_on, r.owner) for r in rows] == [
        ("OPP-6205", "2026-11-09", ""), ("OPP-6201", "2026-11-10", ""), ("OPP-6202", "2026-11-06", "Ben")]
    # an Answered note takes a case off; a noted reply, an FU1 case, another
    # office's case, a snoozed one, a held one and one the scan wrote (FX4)
    # are not here
    assert not {"OPP-6203", "OPP-6204", "OPP-6206", "OPP-6207", "OPP-6208", "OPP-6209",
                "OPP-6210"} & {r.opp_no for r in rows}
    # a case is on one tab at most
    assert not {r.opp_no for r in rows} & {r.opp_no for r in assign_list.listed(w["conn"], TODAY)}


# ------------------------------------------------------- What They Asked --
def test_what_they_asked_from_the_reply_mail(world):
    w = world
    store(w, mail("r6201", "Dear Ines team,\nThanks for the quote. Could you add the THC at destination? "
                  "Please also provide the free time at <https://heronsgate.example/terms?x=1>. "
                  "Our client reads it meanwhile.\nBest regards,\nInes Rook\nHeronsgate Cargo\n\n"
                  "发件人： JJC-DESK <desk@jojofreight.example>\nAny questions? Let us know."))
    note = asks("Ines Rook", "Thanks for the quote.", "r6201")
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        text = assign_list.what_they_asked(note, search)
    assert text == "Ines Rook: Could you add the THC at destination? Please also provide the free time at."
    # the 160 cut, at a sentence end
    long = "Could you quote 2 x 40HQ to Arica? " + "Can you also check the inland leg to Calama with the trucker? " * 3
    assert assign_list.asked_words(long) == ("Could you quote 2 x 40HQ to Arica? Can you also check the inland leg to "
                                              "Calama with the trucker? ...")
    assert len(assign_list.asked_words("Could you " + "check this lane " * 20 + "?")) <= assign_list.ASKED_MAX
    assert assign_list.asked_words("Hi Ines,\nPlease advise TT & FT, thanks") == "Please advise TT & FT"
    assert assign_list.asked_words("Hi Ines,\nPlease advise the cut-off\nThanks & Best Regards\nInes") == \
        "Please advise the cut-off"
    assert assign_list.asked_words("The airport is SCL, please also provide the consignee，") == \
        "The airport is SCL, please also provide the consignee"
    # no such mail: the note's own words
    assert assign_list.what_they_asked(asks("Ines Rook", "Can you quote LCL too?", "missing"), None) == \
        "Ines Rook: Can you quote LCL too?"
