"""Batch T6: a mail from another of our offices is not ours. One reading
(propose.office_side) for propose and detect_followups: on our domain a
mail is the requester office's (its prefix), ours (no office code, or the
office handling the case), or another office's - a vendor answering us.

Every company, person, office code, mail and date here is invented; cases
from OPP-8201.
"""

from __future__ import annotations

import pytest

from conftest import add_enquiry, add_party, seed_reference
from opplist import db, detect_followups, mail_archive, parsing, propose, silent
from opplist.outlook_bridge import MailInfo

INTERNAL = {"jojofreight.example"}
CUSTOMER = ("orla@thistlecombe.test", "Orla Penrose")
US = ("rafe@jojofreight.example", "JJC-RAFE MORROW")
ORIGIN = ("nora@jojofreight.example", "QXA-NORA VALE")           # an origin office asked for its costs
HANDLER = ("ivo@jojofreight.example", "YQX-IVO DARR")            # the office handling the case
SUBJECT = "RATE REQUEST // NINGBO - CHICAGO // LCL"


def info(at, sender, body, mid, to=(), internal=None, subject="RE: " + SUBJECT, conversation="CONV-8201"):
    return MailInfo(subject=subject, received_on=at[:10], received_at=at, sent_at=at, entry_id="EID-" + mid,
                    conversation_id=conversation, conversation_topic=parsing.clean_subject(subject),
                    sender_name=sender[1], sender_address=sender[0],
                    sender_internal=sender[0].endswith("@jojofreight.example") if internal is None else internal,
                    to_addresses=[a for a, _ in to], to_names=[n for _, n in to], body_excerpt=body,
                    folder_path="\\Mailbox\\Inbox", display_to="; ".join(n for _, n in to),
                    message_id="<{}@x>".format(mid))


def read(mails, requester="Thistlecombe Ceramics", domains=("thistlecombe.test",), handled_by=""):
    office = propose.office_prefix(requester)
    thread = [propose.to_thread_mail(m, INTERNAL, set(domains), office, handled_by) for m in mails]
    return propose.classify(thread, requester, requester_known=bool(domains or office))


# ------------------------------------------------------------ the reading --
@pytest.mark.parametrize("name, office, handled, side", [
    ("JJC-RAFE MORROW", "", "", "ours"),
    ("Rafe Morrow", "", "", "ours"),
    ("QXA-NORA VALE", "", "", "other office"),
    ("YQX-IVO DARR", "", "YQX", "ours"),
    ("QVR-INES BALD", "QVR-", "", "requester"),
    ("QXA-NORA VALE", "QVR-", "YQX", "other office"),
])
def test_one_reading_of_a_mail_on_our_domain(name, office, handled, side):
    assert propose.office_side(name, office, handled) == side


# ---------------------------------------------------------------- propose --
def test_an_origin_office_s_costs_are_no_quote_and_ours_after_it_is():
    costs = info("2026-11-03 08:00", ORIGIN, "Hi Rafe,\nOur buying level at the moment:\nUSD 41/cbm", "c1",
                 to=(US,))
    got = read([costs])
    assert got.proposed_status != "Quoted"
    quote = info("2026-11-03 11:00", US, "Hi Orla,\nPlease see below rates for your reference.\nUSD 58/cbm",
                 "q1", to=(CUSTOMER,))
    got = read([costs, quote])
    assert (got.proposed_status, got.evidence_sender) == ("Quoted", "JJC-RAFE MORROW <rafe@jojofreight.example>")


def test_a_own_office_as_requester_is_still_the_requester():
    office = ("ines@jojofreight.example", "QVR-INES BALD")
    mail = info("2026-11-05 09:00", office, "Hi Rafe,\nPlease proceed with the booking.\nInes", "r1", to=(US,))
    thread = propose.to_thread_mail(mail, INTERNAL, set(), propose.office_prefix("JoJo QVR"))
    assert (thread.internal, thread.from_requester, thread.other_office) == (False, True, False)
    assert read([mail], requester="JoJo QVR", domains=()).proposed_status == "Won"


def test_the_handling_office_s_quote_to_the_requester_is_ours():
    quote = info("2026-11-04 10:00", HANDLER, "Hi Orla,\nPlease see below rates for your reference.\nUSD 60/cbm",
                 "h1", to=(CUSTOMER,))
    assert read([quote], handled_by="YQX").proposed_status == "Quoted"
    assert read([quote]).proposed_status != "Quoted"                     # not handling it: another office's


@pytest.mark.parametrize("domains", [("thistlecombe.test",), ()])
def test_another_office_s_please_proceed_is_no_won(domains):
    mail = info("2026-11-06 09:00", ORIGIN, "Hi Rafe,\nPlease proceed, the shipper confirmed.\nNora", "p1", to=(US,))
    assert read([mail], domains=domains).proposed_status != "Won"


# ------------------------------------------------------ detect_followups --
@pytest.fixture
def world(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "mail.db"
    archive.parent.mkdir()
    monkeypatch.setattr(mail_archive, "ARCHIVE_DB", archive)
    conn = db.open_db(tmp_path / "t6.db")
    repo = db.Repository(conn, source="test")
    seed_reference(repo)
    db.set_setting(conn, "internal_domains", ["jojofreight.example"])
    office = add_party(repo, "JoJo QVR", ptype="Office", country="Chile")
    customer = add_party(repo, "Thistlecombe Ceramics", ptype="Direct Customer", country="USA")
    repo.insert("party_domain", {"domain": "thistlecombe.test", "party_id": customer})
    ids = {
        "qvr": add_enquiry(repo, "OPP-8201", office, received="2026-11-02", source_subject=SUBJECT,
                           thread_key=parsing.subject_thread_key(SUBJECT), conversation_id="CONV-8201",
                           service_mode="LCL", origin_country="USA", dest_country="Chile"),
        "rdc": add_enquiry(repo, "OPP-8202", customer, received="2026-11-02", source_subject="LCL NINGBO 8202",
                           thread_key=parsing.subject_thread_key("LCL NINGBO 8202"), conversation_id="CONV-8202",
                           service_mode="LCL", origin_country="China", dest_country="USA"),
    }
    repo.set_status(ids["qvr"], "Quoted", "2026-11-03")
    repo.set_status(ids["rdc"], "Quoted", "2026-11-03")
    repo.commit()

    def store(*mails):
        aconn = mail_archive.open_archive(archive)
        mail_archive.store(aconn, [mail_archive.record_from_info(m) for m in mails])
        aconn.commit()
        aconn.close()

    def row(key):
        return conn.execute("SELECT * FROM v_enquiry WHERE id = ?", (ids[key],)).fetchone()

    yield {"conn": conn, "store": store, "row": row, "archive": archive}
    conn.close()


def test_a_requester_office_s_amount_to_its_own_colleagues_is_no_requote_of_ours(world):
    w = world
    ines = ("ines@jojofreight.example", "QVR-INES BALD")
    colleague = ("tomas@jojofreight.example", "QVR-TOMAS ARCE")
    w["store"](
        info("2026-11-03 10:00", US, "Hi Ines,\nOur rate is USD 2,100 per 40HQ all-in.\nRafe", "q1", to=(ines,)),
        info("2026-11-06 10:00", ines, "Tomas,\nOur rate to the client is USD 2,400 per 40HQ all-in.\nInes", "x1",
             to=(colleague,)))
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        assert detect_followups.requoted_mails(w["conn"], search, w["row"]("qvr")) == []
        # ours, revised, to the requester office: still a re-quote
        w["store"](info("2026-11-08 10:00", US, "Hi Ines,\nRevised rate: USD 1,950 per 40HQ all-in.\nRafe", "q2",
                        to=(ines,)))
        found = detect_followups.requoted_mails(w["conn"], search, w["row"]("qvr"))
    assert [r.at for r in found] == ["2026-11-08"]


def test_another_office_s_mail_on_the_quote_day_does_not_move_the_quote_date(world):
    w = world
    w["store"](
        info("2026-11-03 08:00", ORIGIN, "Hi Rafe,\nOur buying level at the moment:\nUSD 41/cbm", "c2",
             to=(US,), subject="RE: LCL NINGBO 8202", conversation="CONV-8202"),
        info("2026-11-03 11:00", US, "Hi Orla,\nPlease see below rates.\nUSD 58/cbm", "q3", to=(CUSTOMER,),
             subject="RE: LCL NINGBO 8202", conversation="CONV-8202"))
    with mail_archive.ArchiveSearch(w["archive"]) as search:
        got = silent.reading(w["conn"], search, w["row"]("rdc"), detect_followups.internal_domains(w["conn"]))
        ours, theirs = detect_followups._sides(w["row"]("rdc"), {"jojofreight.example"}, {"thistlecombe.test"})
    assert got["quote_stamp"] == "2026-11-03 11:00"
    origin = info("2026-11-03 08:00", ORIGIN, "x", "c3")
    assert not ours(origin) and not theirs(origin)                      # neither ours nor theirs
